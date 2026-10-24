"""Verifiable zip bundles with a SHA-256 manifest.

The manifest lists every other zip member. Its own SHA-256 is not stored
inside the manifest; callers return that digest beside the bundle (the export
API puts it in ``X-Qingyu-Manifest-SHA256``).
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import io
import json
from pathlib import Path
import stat
import zipfile


FORMAT = "qingyu-export-bundle-v1"
MANIFEST_NAME = "manifest.json"
_ZIP_DATE = (1980, 1, 1, 0, 0, 0)
_CHUNK = 1024 * 1024


class ExportBundleError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class BundleInput:
    """One payload member. Exactly one of ``data`` or ``source`` is set."""

    path: str
    data: bytes | None = None
    source: Path | None = None
    expected_sha256: str | None = None
    expected_size: int | None = None


def canonical_json(value: object) -> bytes:
    """UTF-8 canonical JSON with sorted keys and a trailing newline."""
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        + "\n"
    ).encode("utf-8")


def safe_bundle_path(path: str) -> str:
    if not isinstance(path, str) or not path or path.startswith("/") or "\\" in path:
        raise ExportBundleError("unsafe_bundle_path")
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ExportBundleError("unsafe_bundle_path")
    if path == MANIFEST_NAME:
        raise ExportBundleError("unsafe_bundle_path")
    return path


def _zip_info(path: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(filename=path, date_time=_ZIP_DATE)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o644) << 16
    return info


def _write_member(archive: zipfile.ZipFile, path: str, source: BundleInput) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with archive.open(_zip_info(path), "w") as output:
        if source.data is not None:
            output.write(source.data)
            digest.update(source.data)
            size = len(source.data)
        else:
            assert source.source is not None
            with source.source.open("rb") as handle:
                while chunk := handle.read(_CHUNK):
                    output.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
    actual = digest.hexdigest()
    if source.expected_sha256 is not None and actual != source.expected_sha256:
        raise ExportBundleError("digest_mismatch")
    if source.expected_size is not None and size != source.expected_size:
        raise ExportBundleError("digest_mismatch")
    return actual, size


def build_bundle(
    destination: Path,
    *,
    scope: str,
    conversation_id: str | None,
    generated_at: str,
    high_water: dict | None,
    files: list[BundleInput],
) -> str:
    """Write ``destination`` and return the SHA-256 of the manifest bytes."""
    if scope not in {"conversation", "archive"}:
        raise ExportBundleError("invalid_scope")
    if scope == "conversation" and not conversation_id:
        raise ExportBundleError("conversation_required")
    if scope == "archive" and conversation_id is not None:
        raise ExportBundleError("archive_scope_has_no_conversation")
    seen: set[str] = set()
    prepared: list[BundleInput] = []
    for item in files:
        path = safe_bundle_path(item.path)
        if path in seen:
            raise ExportBundleError("duplicate_bundle_path")
        seen.add(path)
        has_data = item.data is not None
        has_source = item.source is not None
        if has_data == has_source:
            raise ExportBundleError("invalid_bundle_source")
        prepared.append(
            BundleInput(
                path=path,
                data=item.data,
                source=item.source,
                expected_sha256=item.expected_sha256,
                expected_size=item.expected_size,
            )
        )
    prepared.sort(key=lambda item: item.path)

    listed: list[dict[str, object]] = []
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w") as archive:
        for item in prepared:
            digest, size = _write_member(archive, item.path, item)
            listed.append({"path": item.path, "sha256": digest, "size_bytes": size})
        manifest = {
            "format": FORMAT,
            "scope": scope,
            "conversation_id": conversation_id,
            "generated_at": generated_at,
            "evidence_contract": "evidence-query-v1",
            "consistency": "bounded_high_water_enumeration_v1",
            "high_water": high_water,
            "files": listed,
        }
        manifest_bytes = canonical_json(manifest)
        with archive.open(_zip_info(MANIFEST_NAME), "w") as output:
            output.write(manifest_bytes)
    return hashlib.sha256(manifest_bytes).hexdigest()


def verify_bundle(blob: bytes) -> dict:
    """Recompute every payload digest and the manifest digest.

    Returns ``{"manifest": ..., "manifest_sha256": ...}``. Raises
    ``ExportBundleError`` when the bundle is incomplete or does not match.
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile as exc:
        raise ExportBundleError("invalid_zip") from exc
    with archive:
        names = archive.namelist()
        if MANIFEST_NAME not in names or names.count(MANIFEST_NAME) != 1:
            raise ExportBundleError("manifest_missing")
        if any(name.endswith("/") for name in names):
            raise ExportBundleError("unexpected_directory_entry")
        manifest_bytes = archive.read(MANIFEST_NAME)
        try:
            manifest = json.loads(manifest_bytes)
        except json.JSONDecodeError as exc:
            raise ExportBundleError("manifest_invalid") from exc
        if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
            raise ExportBundleError("format_mismatch")
        listed = manifest.get("files")
        if not isinstance(listed, list):
            raise ExportBundleError("manifest_invalid")
        listed_paths: list[str] = []
        checked: list[tuple[str, str, int]] = []
        for item in listed:
            if not isinstance(item, dict):
                raise ExportBundleError("manifest_invalid")
            path = item.get("path")
            expected = item.get("sha256")
            size = item.get("size_bytes")
            if (
                not isinstance(path, str)
                or not isinstance(expected, str)
                or isinstance(size, bool)
                or not isinstance(size, int)
                or size < 0
            ):
                raise ExportBundleError("manifest_invalid")
            safe_bundle_path(path)
            listed_paths.append(path)
            checked.append((path, expected, size))
        if len(listed_paths) != len(set(listed_paths)):
            raise ExportBundleError("duplicate_manifest_path")
        payload_names = [name for name in names if name != MANIFEST_NAME]
        if sorted(payload_names) != sorted(listed_paths):
            raise ExportBundleError("manifest_file_set_mismatch")
        for path, expected, size in checked:
            data = archive.read(path)
            actual = hashlib.sha256(data).hexdigest()
            if actual != expected or len(data) != size:
                raise ExportBundleError("digest_mismatch")
        return {
            "manifest": manifest,
            "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        }
