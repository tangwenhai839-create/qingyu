
import pytest
from app.importers.generic import (
    GenericTreeParser,
    ParserValidationError,
    canonical_attachment_descriptor,
)
from app.importers.revision import compute_revision_fingerprint


def _message_payload(attachments):
    return {
        "schema_marker": "qingyu-fixture-v1",
        "conversations": [{
            "id": "conversation",
            "mapping": {
                "node/one": {
                    "id": "node/one",
                    "parent": None,
                    "message": {
                        "author": {"role": "user"},
                        "content": {"parts": ["hello"]},
                        "attachments": attachments,
                    },
                },
            },
        }],
    }


def _fingerprint(*, attachments=()):
    return compute_revision_fingerprint(
        role="user",
        content_text="hello",
        content_json={"parts": ["hello"]},
        model=None,
        raw_timestamp=None,
        attachments=attachments,
    )


def test_parser_carries_stable_declarations_and_observation_pointer():
    parsed = next(GenericTreeParser().parse(_message_payload([{
        "id": " external-1 ",
        "reference": r" refs\one ",
        "member_path": r".\files\.\one.bin",
        "filename": "one.bin",
        "media_type": "application/octet-stream",
        "size_bytes": 7,
        "metadata": {"declared": True},
    }])))

    attachment = parsed.messages[0].attachments[0]
    assert attachment.stable_attachment_key == "external:external-1"
    assert attachment.external_attachment_id == "external-1"
    assert attachment.original_reference == "refs/one"
    assert attachment.member_path == "files/one.bin"
    assert attachment.raw_json_pointer == "/conversations/0/mapping/node~1one/message/attachments/0"
    assert attachment.descriptor_json == {
        "filename": "one.bin",
        "id": "external-1",
        "media_type": "application/octet-stream",
        "member_path": "files/one.bin",
        "metadata": {"declared": True},
        "reference": "refs/one",
        "size_bytes": 7,
    }


def test_anonymous_occurrences_do_not_depend_on_other_descriptor_order():
    repeated = {"filename": "same.bin", "media_type": "application/octet-stream"}
    other = {"filename": "other.bin"}

    forward = next(GenericTreeParser().parse(_message_payload([repeated, other, dict(repeated)]))).messages[0].attachments
    reordered = next(GenericTreeParser().parse(_message_payload([other, repeated, dict(repeated)]))).messages[0].attachments

    forward_keys = [item.stable_attachment_key for item in forward if item.filename == "same.bin"]
    reordered_keys = [item.stable_attachment_key for item in reordered if item.filename == "same.bin"]
    assert forward_keys == reordered_keys
    assert forward_keys[0].endswith(":occurrence:1")
    assert forward_keys[1].endswith(":occurrence:2")


def test_attachment_descriptors_are_stably_sorted_in_revision_fingerprint():
    first = {"stable_attachment_key": "external:a", "id": "a", "filename": "a.bin"}
    second = {"stable_attachment_key": "external:b", "id": "b", "filename": "b.bin"}
    assert _fingerprint(attachments=[first, second]) == _fingerprint(attachments=[second, first])
    assert _fingerprint(attachments=[first]) != _fingerprint(attachments=[second])


def test_runtime_observation_fields_cannot_change_revision_fingerprint():
    declared = {"stable_attachment_key": "external:a", "id": "a", "filename": "a.bin"}
    observed = {
        **declared,
        "availability_status": "available",
        "archive_object_id": "runtime-id",
        "archive_sha256": "f" * 64,
        "import_job_id": "job-id",
        "attempt_count": 2,
        "observed_mime": "runtime/type",
        "observed_size": 999,
        "raw_json_pointer": "/runtime/path",
    }
    assert _fingerprint(attachments=[declared]) == _fingerprint(attachments=[observed])


def test_no_attachments_preserves_legacy_fingerprint_payload():
    legacy = compute_revision_fingerprint(
        role="user",
        content_text="hello",
        content_json={"parts": ["hello"]},
        model=None,
        raw_timestamp=None,
    )
    assert legacy == _fingerprint()
    assert legacy == _fingerprint(attachments=[])
    assert legacy == "64b02eb4b97851831f38d132a0eb933e38c0e1cb7d201f5169ebee0932a0bd7e"


def test_canonical_descriptor_ignores_observation_fields():
    base = {"id": "a", "filename": "a.bin"}
    assert canonical_attachment_descriptor(base) == canonical_attachment_descriptor({
        **base,
        "availability_status": "missing",
        "archive_object_id": "not-source-declared",
    })


@pytest.mark.parametrize("member_path", [
    "/absolute.bin",
    r"C:\drive.bin",
    r"\\server\share.bin",
    "safe/../escape.bin",
    "safe/name:stream",
    ".",
    "  ",
])
def test_parser_rejects_unsafe_or_empty_attachment_member_paths(member_path):
    with pytest.raises(ParserValidationError) as raised:
        next(GenericTreeParser().parse(_message_payload([{
            "id": "even-named-attachments-must-be-checked",
            "member_path": member_path,
        }])))
    assert raised.value.errors[0]["type"] == "invalid_attachment_member_path"


def test_reference_normalization_does_not_fold_url_path_semantics():
    parsed = next(GenericTreeParser().parse(_message_payload([{
        "reference": r" https://example.test/a/../b\asset.bin?x=1 ",
    }])))
    attachment = parsed.messages[0].attachments[0]
    assert attachment.original_reference == "https://example.test/a/../b/asset.bin?x=1"
    assert attachment.stable_attachment_key == "reference:https://example.test/a/../b/asset.bin?x=1"
