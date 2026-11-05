"""Default Compose archive root is a subdirectory of the volume mount.

``ensure_staging_layout`` and staging cleanup reject a mount point
(``os.path.ismount`` → ``archive_root_is_unsafe`` / ``unsafe_archive_root``).
The ``archive_data`` volume is mounted at ``/data``. App and worker set
``ARCHIVE_ROOT=/data/archive`` so a default ``docker compose up`` can ingest.
The trees under that root stay ``staging/``, ``archive/objects/``, and
``manifests/``.
"""

import os
import re
from pathlib import Path, PurePosixPath

import pytest
from app.archive.manifest import manifest_path
from app.archive.service import ArchiveService
from app.importers.cleanup import cleanup_staging
from app.importers.staging import UnsafeStagingRoot, ensure_staging_layout

GATEWAY = Path(__file__).resolve().parents[1]
COMPOSE = GATEWAY / "docker-compose.yml"
DOCKERFILE = GATEWAY / "Dockerfile"
VOLUME_MOUNT = "/data"
ARCHIVE_ROOT = "/data/archive"
_SHA = "ab" * 32


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _compose_without_comments(text: str) -> str:
    return "\n".join(line.split("#", 1)[0] for line in text.splitlines())


def test_compose_archive_root_is_a_subdirectory_of_the_volume_mount():
    body = _compose_without_comments(COMPOSE.read_text(encoding="utf-8"))
    roots = [
        _unquote(value)
        for value in re.findall(r"(?m)^[ ]+ARCHIVE_ROOT:[ ]*(\S+)[ ]*$", body)
    ]
    mounts = re.findall(r"(?m)^[ ]+-[ ]*archive_data:(\S+)[ ]*$", body)
    assert roots == [ARCHIVE_ROOT, ARCHIVE_ROOT]
    assert mounts == [VOLUME_MOUNT, VOLUME_MOUNT]
    root = PurePosixPath(roots[0])
    mount = PurePosixPath(mounts[0])
    assert root.is_absolute() and mount.is_absolute()
    assert root != mount
    assert mount in root.parents


def test_image_creates_archive_root_before_dropping_privileges():
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    mkdir_at = dockerfile.index("mkdir -p /data/archive")
    user_at = dockerfile.index("\nUSER qingyu")
    assert mkdir_at < user_at
    assert "chown qingyu:qingyu /data /data/archive" in dockerfile


def test_mount_point_archive_root_is_unsafe_and_child_keeps_layout(tmp_path, monkeypatch):
    mount = tmp_path / "data"
    mount.mkdir()
    child = mount / "archive"
    real_ismount = os.path.ismount

    def ismount(path):
        return os.path.abspath(os.fspath(path)) == os.path.abspath(mount) or real_ismount(path)

    monkeypatch.setattr(os.path, "ismount", ismount)

    with pytest.raises(UnsafeStagingRoot, match="^archive_root_is_unsafe$"):
        ensure_staging_layout(mount)
    assert not (mount / "staging").exists()

    refused = cleanup_staging(mount, None)
    assert refused.skipped_unknown == [Path(os.path.abspath(mount / "staging"))]
    assert refused.counts["deleted"] == 0

    layout = ensure_staging_layout(child)
    assert layout.root == Path(os.path.abspath(child))
    assert layout.staging == layout.root / "staging"
    for name in ("uploads", "archive-prepared", "jobs", "quarantine"):
        assert (layout.staging / name).is_dir()
    assert not (mount / "staging").exists()

    assert not (child / "archive").exists()
    assert not (child / "manifests").exists()

    service = ArchiveService(child, None)
    assert service.staging == child / "staging"
    assert service.objects == child / "archive" / "objects"
    assert service.manifests == child / "manifests"
    published = manifest_path(child, _SHA)
    assert published == child / "manifests" / f"{_SHA}.json"
    assert layout.root in published.parents

    object_file = child / "archive" / "objects" / _SHA[:2] / _SHA
    object_file.parent.mkdir(parents=True)
    object_file.write_bytes(b"keep")
    published.parent.mkdir(parents=True)
    published.write_text("keep", encoding="utf-8")
    accepted = cleanup_staging(child, None)
    assert accepted.skipped_unknown == []
    assert accepted.counts["deleted"] == 0
    assert object_file.read_bytes() == b"keep"
    assert published.read_text(encoding="utf-8") == "keep"
