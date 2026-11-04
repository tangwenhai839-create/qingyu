"""Durable map from a caller idempotency key to one gateway client_request_id.

The file is an append-only log, not a queue. Nothing here replays a write.
A request id is minted only after the new line is fsynced, and the HTTP call
happens only after that returns. A later process that sees the same key
reuses the id. The id is a random UUID. It is never a digest of the body.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import sys
import uuid
from pathlib import Path

_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_REQUEST_ID = re.compile(
    r"^mcp-[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_KINDS = frozenset({"message", "conversation"})


class PendingLogError(Exception):
    """The pending log cannot be used. The message is a stable code."""


class PendingKindConflict(PendingLogError):
    """The key is already bound to the other ingest tool."""


def new_client_request_id(*forbidden: str) -> str:
    """Mint a ledger id. The result is not a SHA-256 digest and not `forbidden`."""
    blocked = {item for item in forbidden if item}
    for _ in range(5):
        candidate = f"mcp-{uuid.uuid4()}"
        if candidate in blocked or _REQUEST_ID.fullmatch(candidate) is None:
            continue
        if re.fullmatch(r"[0-9a-f]{64}", candidate):
            continue
        return candidate
    raise PendingLogError("request_id_unavailable")


class PendingLog:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.lock_path = Path(str(self.path) + ".lock")

    def assign(self, idempotency_key: str, *, kind: str) -> str:
        if kind not in _KINDS:
            raise PendingLogError("invalid_pending_kind")
        if _KEY.fullmatch(idempotency_key) is None:
            raise PendingLogError("invalid_idempotency_key")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise PendingLogError("pending_log_unreadable") from None

        try:
            lock_fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError:
            raise PendingLogError("pending_log_unreadable") from None
        try:
            _lock(lock_fd)
            try:
                return self._assign_locked(idempotency_key, kind)
            finally:
                _unlock(lock_fd)
        finally:
            os.close(lock_fd)

    def _assign_locked(self, idempotency_key: str, kind: str) -> str:
        try:
            data_fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError:
            raise PendingLogError("pending_log_unreadable") from None
        try:
            raw = _read_text(data_fd)
            existing = _index(_parse(raw))
            found = existing.get(idempotency_key)
            if found is not None:
                request_id, stored_kind = found
                if stored_kind != kind:
                    raise PendingKindConflict("idempotency_key_bound_to_other_tool")
                return request_id
            request_id = new_client_request_id(idempotency_key)
            line = json.dumps(
                {
                    "v": 1,
                    "idempotency_key": idempotency_key,
                    "client_request_id": request_id,
                    "kind": kind,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            _write_all(data_fd, (line + "\n").encode("utf-8"))
            os.fsync(data_fd)
        finally:
            os.close(data_fd)
        _fsync_dir(self.path.parent)
        return request_id


def _parse(raw: str) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for line in _complete_lines(raw):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            raise PendingLogError("pending_log_corrupt") from None
        if not isinstance(value, dict):
            raise PendingLogError("pending_log_corrupt")
        record = _record(value)
        records.append(record)
    return records


def _record(value: dict) -> dict[str, str]:
    try:
        version = value["v"]
        key = value["idempotency_key"]
        request_id = value["client_request_id"]
        kind = value["kind"]
    except KeyError:
        raise PendingLogError("pending_log_corrupt") from None
    if version != 1 or not isinstance(key, str) or not isinstance(request_id, str):
        raise PendingLogError("pending_log_corrupt")
    if not isinstance(kind, str) or kind not in _KINDS:
        raise PendingLogError("pending_log_corrupt")
    if _KEY.fullmatch(key) is None or _REQUEST_ID.fullmatch(request_id) is None:
        raise PendingLogError("pending_log_request_id_rejected")
    return {
        "idempotency_key": key,
        "client_request_id": request_id,
        "kind": kind,
    }


def _index(records: list[dict[str, str]]) -> dict[str, tuple[str, str]]:
    found: dict[str, tuple[str, str]] = {}
    for record in records:
        pair = (record["client_request_id"], record["kind"])
        previous = found.get(record["idempotency_key"])
        if previous is not None and previous != pair:
            raise PendingLogError("pending_log_conflict")
        found[record["idempotency_key"]] = pair
    return found


def _complete_lines(raw: str) -> list[str]:
    """Drop a trailing partial line. A crash mid-write must not mint a bad id."""
    if not raw:
        return []
    if not raw.endswith("\n"):
        raw, _sep, _tail = raw.rpartition("\n")
    lines: list[str] = []
    for line in raw.split("\n"):
        if line.strip():
            lines.append(line)
    return lines


def _read_text(fd: int) -> str:
    """Return complete lines. A trailing partial line is truncated away.

    The partial line was never fsynced as a record, so it must not sit in
    front of the next append and make that append unreadable.
    """
    os.lseek(fd, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while True:
        block = os.read(fd, 65536)
        if not block:
            break
        chunks.append(block)
    try:
        raw = b"".join(chunks).decode("utf-8")
    except UnicodeDecodeError:
        raise PendingLogError("pending_log_corrupt") from None
    if raw.endswith("\n") or raw == "":
        os.lseek(fd, 0, os.SEEK_END)
        return raw
    head, sep, _tail = raw.rpartition("\n")
    kept = f"{head}{sep}" if sep else ""
    os.ftruncate(fd, len(kept.encode("utf-8")))
    os.fsync(fd)
    os.lseek(fd, 0, os.SEEK_END)
    return kept


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise PendingLogError("pending_log_unreadable")
        view = view[written:]


def _fsync_dir(path: Path) -> None:
    try:
        dir_fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        return
    finally:
        os.close(dir_fd)


def _lock(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        return
    fcntl.flock(fd, fcntl.LOCK_EX)


def _unlock(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return
    fcntl.flock(fd, fcntl.LOCK_UN)
