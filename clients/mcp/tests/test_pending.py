"""The pending log keeps one client_request_id across processes."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from qingyu_mcp.pending import (
    PendingKindConflict,
    PendingLog,
    PendingLogError,
    new_client_request_id,
)


def test_request_id_is_a_uuid_and_not_a_digest() -> None:
    digest = hashlib.sha256(b'{"content_text":"remember"}').hexdigest()
    minted = new_client_request_id(digest)
    assert minted.startswith("mcp-")
    assert minted != digest
    assert len(minted) != 64


def test_same_key_reuses_the_id_and_a_hash_key_is_not_the_id(tmp_path: Path) -> None:
    path = tmp_path / "pending.jsonl"
    digest = hashlib.sha256(b"remember the backup window").hexdigest()
    first = PendingLog(path).assign(digest, kind="message")
    second = PendingLog(path).assign(digest, kind="message")
    assert first == second
    assert first != digest
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["idempotency_key"] == digest
    assert record["client_request_id"] == first
    assert record["kind"] == "message"
    assert "payload_hash" not in record


def test_different_keys_get_different_ids(tmp_path: Path) -> None:
    log = PendingLog(tmp_path / "pending.jsonl")
    assert log.assign("key-a", kind="message") != log.assign("key-b", kind="message")


def test_key_cannot_change_tools(tmp_path: Path) -> None:
    log = PendingLog(tmp_path / "pending.jsonl")
    kept = log.assign("op-1", kind="message")
    with pytest.raises(PendingKindConflict, match="idempotency_key_bound_to_other_tool"):
        log.assign("op-1", kind="conversation")
    assert PendingLog(log.path).assign("op-1", kind="message") == kept


def test_invalid_key_does_not_create_the_log(tmp_path: Path) -> None:
    path = tmp_path / "pending.jsonl"
    with pytest.raises(PendingLogError, match="invalid_idempotency_key"):
        PendingLog(path).assign("not a key", kind="message")
    assert not path.exists()


def test_torn_tail_does_not_drop_a_committed_id(tmp_path: Path) -> None:
    path = tmp_path / "pending.jsonl"
    first = PendingLog(path).assign("op-1", kind="message")
    path.write_bytes(path.read_bytes() + b'{"torn"')
    assert PendingLog(path).assign("op-1", kind="message") == first
    other = PendingLog(path).assign("op-2", kind="conversation")
    assert other != first
    assert PendingLog(path).assign("op-1", kind="message") == first


def test_corrupt_line_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "pending.jsonl"
    path.write_text('{"v": 1, "nope": true}\n', encoding="utf-8")
    with pytest.raises(PendingLogError, match="pending_log_corrupt"):
        PendingLog(path).assign("op-1", kind="message")


def test_stored_hash_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "pending.jsonl"
    digest = "ab" * 32
    path.write_text(
        json.dumps(
            {
                "v": 1,
                "idempotency_key": "op-1",
                "client_request_id": digest,
                "kind": "message",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(PendingLogError, match="pending_log_request_id_rejected"):
        PendingLog(path).assign("op-1", kind="message")


def test_concurrent_calls_share_one_id(tmp_path: Path) -> None:
    path = tmp_path / "pending.jsonl"
    found: list[str] = []

    def work() -> None:
        found.append(PendingLog(path).assign("same-key", kind="message"))

    threads = [threading.Thread(target=work) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(set(found)) == 1
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 1


def test_subprocess_restart_reuses_the_id(tmp_path: Path) -> None:
    path = tmp_path / "pending.jsonl"
    first = _assign_in_subprocess(path, "op-restart")
    second = _assign_in_subprocess(path, "op-restart")
    third = _assign_in_subprocess(path, "op-other")
    assert first == second
    assert first != third
    assert first.startswith("mcp-")
    digest = hashlib.sha256(first.encode("utf-8")).hexdigest()
    assert first != digest


def _assign_in_subprocess(path: Path, key: str) -> str:
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys\n"
            "from qingyu_mcp.pending import PendingLog\n"
            "print(PendingLog(sys.argv[1]).assign(sys.argv[2], kind='message'))\n",
            str(path),
            key,
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return completed.stdout.strip()
