import hashlib
import io
import zipfile
from pathlib import Path

import pytest

from app.export.bundle import (
    FORMAT,
    MANIFEST_NAME,
    BundleInput,
    ExportBundleError,
    build_bundle,
    canonical_json,
    verify_bundle,
)
from app.export.service import ExportIntegrityError, collect_bounded_observations


def _build(tmp_path: Path, files, **overrides) -> bytes:
    destination = tmp_path / "bundle.zip"
    values = {
        "scope": "conversation",
        "conversation_id": "conv-1",
        "generated_at": "2026-01-02T03:04:05+00:00",
        "high_water": None,
        "files": files,
    }
    values.update(overrides)
    build_bundle(destination, **values)
    return destination.read_bytes()


def test_bundle_manifest_matches_payload_bytes_and_is_deterministic(tmp_path: Path):
    payload = "清语\n".encode()
    files = [BundleInput(path="evidence/conversation-observations.json", data=payload)]
    first = _build(tmp_path, files)
    second = _build(tmp_path, files)

    assert first == second
    verified = verify_bundle(first)
    assert verified["manifest"]["format"] == FORMAT
    assert verified["manifest"]["scope"] == "conversation"
    assert verified["manifest"]["conversation_id"] == "conv-1"
    assert verified["manifest"]["evidence_contract"] == "evidence-query-v1"
    assert verified["manifest"]["consistency"] == "bounded_high_water_enumeration_v1"
    entry = verified["manifest"]["files"][0]
    assert entry == {
        "path": "evidence/conversation-observations.json",
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
    }
    with zipfile.ZipFile(io.BytesIO(first)) as archive:
        assert hashlib.sha256(archive.read(MANIFEST_NAME)).hexdigest() == verified["manifest_sha256"]
        assert archive.read("evidence/conversation-observations.json") == payload


def test_changed_generated_at_keeps_payload_digest_and_changes_manifest(tmp_path: Path):
    payload = b'{"items":[]}\n'
    files = [BundleInput(path="evidence/conversation-observations.json", data=payload)]
    first = verify_bundle(_build(tmp_path, files, generated_at="2026-01-02T00:00:00+00:00"))
    second = verify_bundle(_build(tmp_path, files, generated_at="2026-01-02T00:00:01+00:00"))

    assert first["manifest"]["files"] == second["manifest"]["files"]
    assert first["manifest_sha256"] != second["manifest_sha256"]


def test_verify_rejects_tampered_payload_extra_member_and_dropped_member(tmp_path: Path):
    blob = _build(
        tmp_path,
        [
            BundleInput(path="evidence/conversation-observations.json", data=b"{}"),
            BundleInput(path="archive/referenced-objects.json", data=b"[]"),
        ],
    )
    verify_bundle(blob)

    def rewrite(transform):
        output = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(blob)) as source, zipfile.ZipFile(output, "w") as dest:
            for info in source.infolist():
                data = source.read(info.filename)
                data = transform(info.filename, data)
                if data is None:
                    continue
                dest.writestr(info.filename, data)
        return output.getvalue()

    with pytest.raises(ExportBundleError, match="digest_mismatch"):
        verify_bundle(rewrite(lambda name, data: b"tampered" if name != MANIFEST_NAME else data))
    with pytest.raises(ExportBundleError, match="manifest_file_set_mismatch"):
        verify_bundle(rewrite(lambda name, data: None if name.endswith("referenced-objects.json") else data))

    extra = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(blob)) as source, zipfile.ZipFile(extra, "w") as dest:
        for info in source.infolist():
            dest.writestr(info.filename, source.read(info.filename))
        dest.writestr("evidence/extra.json", b"{}")
    with pytest.raises(ExportBundleError, match="manifest_file_set_mismatch"):
        verify_bundle(extra.getvalue())


def test_builder_rejects_unsafe_paths_and_hash_mismatch(tmp_path: Path):
    with pytest.raises(ExportBundleError, match="unsafe_bundle_path"):
        _build(tmp_path, [BundleInput(path="../evidence.json", data=b"{}")])
    with pytest.raises(ExportBundleError, match="unsafe_bundle_path"):
        _build(tmp_path, [BundleInput(path=MANIFEST_NAME, data=b"{}")])
    with pytest.raises(ExportBundleError, match="digest_mismatch"):
        _build(
            tmp_path,
            [BundleInput(path="objects/ab", data=b"nope", expected_sha256="a" * 64, expected_size=4)],
        )
    with pytest.raises(ExportBundleError, match="archive_scope_has_no_conversation"):
        _build(tmp_path, [], scope="archive", conversation_id="conv-1")


def test_canonical_json_sorts_keys_and_rejects_nan():
    assert canonical_json({"b": 1, "a": "清"}) == '{"a":"清","b":1}\n'.encode()
    with pytest.raises(ValueError):
        canonical_json({"value": float("nan")})


def test_observation_paging_keeps_the_first_high_water_and_stops():
    class Page:
        def __init__(self, items, has_more, upper_bound):
            self.items = items
            self.has_more = has_more
            self.upper_bound = upper_bound

    class Query:
        def __init__(self):
            self.calls = []

        def list_conversation_observations(self, **kwargs):
            self.calls.append(kwargs)
            if kwargs["after"] is None:
                return Page(
                    [{"committed_at": 1, "conversation_observation_id": "a"}],
                    True,
                    ("upper-time", "upper-id"),
                )
            return Page(
                [{"committed_at": 2, "conversation_observation_id": "b"}],
                False,
                ("other-time", "other-id"),
            )

    query = Query()
    items, upper = collect_bounded_observations(query, conversation_id="conv", page_size=1)

    assert [item["conversation_observation_id"] for item in items] == ["a", "b"]
    assert upper == ("upper-time", "upper-id")
    assert query.calls[1]["upper_bound"] == ("upper-time", "upper-id")
    assert query.calls[1]["after"] == (1, "a")
    assert query.calls[0]["conversation_id"] == "conv"


def test_observation_paging_rejects_a_cursor_that_does_not_advance():
    class Page:
        def __init__(self):
            self.items = [{"committed_at": 1, "conversation_observation_id": "a"}]
            self.has_more = True
            self.upper_bound = ("upper", "a")

    class Query:
        def list_conversation_observations(self, **_kwargs):
            return Page()

    with pytest.raises(ExportIntegrityError):
        collect_bounded_observations(Query(), conversation_id=None, page_size=1)
