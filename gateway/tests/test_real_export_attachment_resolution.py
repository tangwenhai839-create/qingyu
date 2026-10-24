from pathlib import Path
from types import SimpleNamespace

import pytest
from app.importers.attachments import prepare_attachment_work
from app.importers.generic import ParsedAttachment, ParsedConversation, ParsedMessage


class RecordingArchive:
    def __init__(self):
        self.calls = []

    def prepare(self, source, original_filename, source_platform, media_type, **kwargs):
        self.calls.append((source, original_filename, source_platform, media_type, kwargs))
        return SimpleNamespace(sha256="0" * 64, size_bytes=source.stat().st_size)


def _attachment(
    stable_key: str,
    *,
    asset_id: str | None = None,
    member_path: str | None = None,
    references=(),
    media_type: str | None = None,
    size_bytes: int | None = None,
):
    return ParsedAttachment(
        stable_attachment_key=stable_key,
        external_attachment_id=asset_id,
        original_reference=None,
        member_path=member_path,
        filename=None,
        media_type=media_type,
        size_bytes=size_bytes,
        metadata={},
        descriptor_json={},
        raw_json_pointer="/0/mapping/node/message/metadata/attachments/0",
        asset_id=asset_id,
        source_references=tuple(references),
    )


def _conversation(*attachments):
    message = ParsedMessage(
        "node",
        None,
        "user",
        None,
        {},
        None,
        None,
        None,
        tuple(attachments),
    )
    return (ParsedConversation("conversation", None, (message,), None),)


def _work(tmp_path: Path, *attachments):
    archive = RecordingArchive()
    result = prepare_attachment_work(
        _conversation(*attachments),
        tmp_path,
        archive,
        source_platform="chatgpt",
        source_member="conversations.json",
        health_check=lambda: None,
    )
    return list(result.values()), archive


def test_opaque_asset_id_resolves_only_one_exact_dat_stem(tmp_path: Path):
    (tmp_path / "opaque-id.dat").write_bytes(b"RIFF\x04\x00\x00\x00WAVE")

    work, archive = _work(
        tmp_path,
        _attachment(
            "asset:opaque-id",
            asset_id="opaque-id",
            references=(
                {"kind": "asset_pointer", "value": "sediment://opaque-id"},
                {"kind": "asset_pointer", "value": "sediment://opaque-id"},
            ),
        ),
    )

    assert work[0].availability == "available"
    assert work[0].resolution_status == "resolved"
    assert work[0].resolved_member_path == "opaque-id.dat"
    assert work[0].observed_media_type == "audio/wav"
    assert archive.calls[0][4]["logical_path"] == "opaque-id.dat"


def test_asset_matching_is_case_sensitive_and_has_no_filename_fallback(tmp_path: Path):
    (tmp_path / "Asset.dat").write_bytes(b"bytes")
    (tmp_path / "display-name.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    work, archive = _work(
        tmp_path,
        _attachment("case", asset_id="asset", references=({"kind": "asset_pointer"},)),
        _attachment("filename", asset_id="display-name", references=({"kind": "asset_pointer"},)),
    )

    assert [(item.availability, item.resolution_status) for item in work] == [
        ("missing", "missing"),
        ("missing", "missing"),
    ]
    assert archive.calls == []


def test_duplicate_exact_stems_are_ambiguous(tmp_path: Path):
    (tmp_path / "one").mkdir()
    (tmp_path / "two").mkdir()
    (tmp_path / "one" / "same.dat").write_bytes(b"one")
    (tmp_path / "two" / "same.dat").write_bytes(b"two")

    work, archive = _work(
        tmp_path,
        _attachment("same", asset_id="same", references=({"kind": "asset_pointer"},)),
    )

    assert work[0].availability == "referenced_only"
    assert work[0].resolution_status == "ambiguous"
    assert archive.calls == []


@pytest.mark.parametrize("asset_id", ["../escape", "folder/asset", r"folder\asset", ".", ""])
def test_path_like_asset_ids_are_unsupported(tmp_path: Path, asset_id: str):
    work, archive = _work(
        tmp_path,
        _attachment("unsafe", asset_id=asset_id, references=({"kind": "asset_pointer"},)),
    )

    assert work[0].availability == "referenced_only"
    assert work[0].resolution_status == "unsupported"
    assert archive.calls == []


def test_descriptor_without_verified_bytes_is_missing(tmp_path: Path):
    work, archive = _work(tmp_path, _attachment("descriptor", asset_id="declared"))

    assert work[0].availability == "missing"
    assert work[0].resolution_status == "missing"
    assert archive.calls == []


def test_reference_without_supported_asset_scheme_is_unsupported(tmp_path: Path):
    work, _archive = _work(
        tmp_path,
        _attachment(
            "unsupported",
            references=({"kind": "asset_pointer", "value": "other://opaque"},),
        ),
    )

    assert work[0].availability == "referenced_only"
    assert work[0].resolution_status == "unsupported"


def test_reference_without_a_resolvable_locator_is_unresolved(tmp_path: Path):
    work, archive = _work(tmp_path, _attachment("reference-only"))

    assert work[0].availability == "referenced_only"
    assert work[0].resolution_status == "unresolved"
    assert archive.calls == []


@pytest.mark.parametrize("member_path", ["../escape.dat", "/absolute.dat", r"folder\asset.dat"])
def test_noncanonical_explicit_member_paths_are_unsupported(tmp_path: Path, member_path: str):
    work, archive = _work(
        tmp_path,
        _attachment("unsafe-member", member_path=member_path),
    )

    assert work[0].availability == "referenced_only"
    assert work[0].resolution_status == "unsupported"
    assert work[0].error_code == "unsafe_attachment_member"
    assert archive.calls == []


def test_unreferenced_dat_is_not_published_as_attachment(tmp_path: Path):
    (tmp_path / "referenced.dat").write_bytes(b"referenced")
    (tmp_path / "unreferenced.dat").write_bytes(b"unreferenced")

    work, archive = _work(tmp_path, _attachment("ref", asset_id="referenced"))

    assert len(work) == 1
    assert work[0].resolved_member_path == "referenced.dat"
    assert len(archive.calls) == 1


def test_declared_size_and_sniffed_mime_are_verified(tmp_path: Path):
    content = b"\x89PNG\r\n\x1a\n" + b"payload"
    (tmp_path / "image.dat").write_bytes(content)

    work, _archive = _work(
        tmp_path,
        _attachment(
            "image",
            asset_id="image",
            media_type="image/png",
            size_bytes=len(content),
        ),
    )

    assert work[0].availability == "available"
    assert work[0].observed_media_type == "image/png"


def test_resolved_bytes_with_integrity_mismatch_remain_failed_not_missing(tmp_path: Path):
    (tmp_path / "sized.dat").write_bytes(b"actual")

    work, archive = _work(
        tmp_path,
        _attachment("sized", asset_id="sized", size_bytes=999),
    )

    assert work[0].availability == "failed"
    assert work[0].resolution_status == "resolved"
    assert work[0].resolved_member_path == "sized.dat"
    assert work[0].error_code == "attachment_size_mismatch"
    assert archive.calls == []
