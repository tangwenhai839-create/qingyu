"""Missing-manifest repair does not replace a file that is already there."""

import json
import uuid
from types import SimpleNamespace

import pytest
from app.archive.manifest import (
    ManifestRepairRefused,
    manifest_is_finalized,
    render_manifest,
    repair_missing_manifests,
)


def _record(sha: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        sha256=sha,
        size_bytes=4,
        relative_path=f"archive/objects/{sha[:2]}/{sha}",
        media_type="application/octet-stream",
        integrity_status="verified",
        integrity_checked_at=None,
        integrity_error=None,
    )


def test_repair_writes_missing_manifest_and_keeps_existing_file(tmp_path):
    sha = "ab" * 32
    record = _record(sha)
    assert manifest_is_finalized(tmp_path, sha) is False

    written, kept = repair_missing_manifests(tmp_path, [record])

    path = tmp_path / "manifests" / f"{sha}.json"
    assert (written, kept) == (1, 0)
    assert json.loads(path.read_text(encoding="utf-8")) == render_manifest(record)
    assert manifest_is_finalized(tmp_path, sha) is True

    path.write_text('{"kept": true}\n', encoding="utf-8")
    written, kept = repair_missing_manifests(tmp_path, [record])
    assert (written, kept) == (0, 1)
    assert path.read_text(encoding="utf-8") == '{"kept": true}\n'


def test_repair_refuses_symlink_manifest(tmp_path):
    sha = "cd" * 32
    manifests = tmp_path / "manifests"
    manifests.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("keep", encoding="utf-8")
    (manifests / f"{sha}.json").symlink_to(outside)

    with pytest.raises(ManifestRepairRefused, match="unsafe"):
        repair_missing_manifests(tmp_path, [_record(sha)])

    assert outside.read_text(encoding="utf-8") == "keep"
    assert manifest_is_finalized(tmp_path, sha) is False
