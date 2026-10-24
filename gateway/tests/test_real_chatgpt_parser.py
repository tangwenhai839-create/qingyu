# RUF015 wants next(iter(...)) for a first element. That raises StopIteration
# instead of IndexError, so these assertions keep the index.
# ruff: noqa: RUF015
import copy
import hashlib
import json
import os
import zipfile
from collections import Counter
from pathlib import Path

import pytest
from app.importers.generic import (
    GenericTreeParser,
    ParserRegistry,
    ParserValidationError,
)
from app.importers.real_chatgpt import ChatGPTExportParser, normalize_timestamp
from app.importers.revision import (
    compute_canonical_fragment_fingerprint,
    compute_semantic_revision_fingerprint,
    compute_source_observation_fingerprint,
)

SAMPLE_SHA256 = "04c54e6f87bd2113c5183ba7a2f24b2694c333e5eab6f42308f654063ee4b040"


def _message(message_id="message-source-id", *, metadata=None, content=None):
    return {
        "id": message_id,
        "author": {"role": "user", "name": None, "metadata": {"volatile_author": 1}},
        "create_time": 1.25,
        "update_time": None,
        "content": content or {"content_type": "text", "parts": ["hello"]},
        "status": "finished_successfully",
        "end_turn": None,
        "weight": 1,
        "recipient": "all",
        "metadata": metadata or {"model_slug": "model", "volatile_export_field": "first"},
    }


def _payload():
    return [{
        "id": "conversation-source-id",
        "conversation_id": "conversation-source-id",
        "title": "synthetic",
        "create_time": 1.0,
        "update_time": 2.0,
        "current_node": "leaf",
        "unknown_conversation_metadata": {"preserved": True},
        "mapping": {
            "root": {"id": "root", "parent": None, "children": ["n/1~x"], "message": None},
            "n/1~x": {
                "id": "n/1~x",
                "parent": "root",
                "children": ["leaf"],
                "message": _message(content={
                    "content_type": "multimodal_text",
                    "parts": [
                        "caption",
                        {
                            "content_type": "image_asset_pointer",
                            "asset_pointer": "sediment://asset-one",
                            "metadata": {"volatile": 1},
                            "expiry_datetime": "temporary",
                        },
                    ],
                }),
            },
            "leaf": {"id": "leaf", "parent": "n/1~x", "children": []},
        },
    }]


def test_registry_keeps_fixture_contract_separate_and_detects_real_array():
    fixture = {"schema_marker": GenericTreeParser.marker, "conversations": []}
    real = _payload()

    fixture_status, fixture_parser = ParserRegistry().detect(fixture, "fixture.json")
    real_status, real_parser = ParserRegistry().detect(real, "conversations.json")

    assert (fixture_status, fixture_parser.name, fixture_parser.version) == (
        "matched", "generic-tree-fixture", "0.1"
    )
    assert (real_status, real_parser.name, real_parser.version) == (
        "matched", "chatgpt-export", "1"
    )
    assert not GenericTreeParser().can_parse(real)
    assert not ChatGPTExportParser().can_parse(fixture)
    assert ParserRegistry().detect([]) == ("unsupported_structure", None)


def test_detection_does_not_hide_a_malformed_later_conversation():
    payload = _payload() + [{"id": "later", "mapping": []}]
    status, parser = ParserRegistry().detect(payload)

    assert status == "matched"
    with pytest.raises(ParserValidationError) as caught:
        parser.validate(payload)
    assert any(error["raw_pointer"] == "/1/mapping" for error in caught.value.errors)
    with pytest.raises(ParserValidationError):
        next(parser.parse(payload))


def test_nodes_messages_and_source_envelopes_are_separate_and_lossless():
    payload = _payload()
    conversation = tuple(ChatGPTExportParser().parse(payload))[0]

    assert [node.node_id for node in conversation.nodes] == ["root", "n/1~x", "leaf"]
    assert [node.message_presence for node in conversation.nodes] == ["null", "object", "absent"]
    assert len(conversation.messages) == 1
    assert conversation.messages[0].node_id == "n/1~x"
    assert conversation.messages[0].parent_node_id == "root"
    assert conversation.messages[0].external_message_id == "message-source-id"
    assert conversation.nodes[0].message is None
    assert conversation.nodes[2].message is None
    assert conversation.nodes[1].raw_json_pointer == "/0/mapping/n~11~0x"
    assert "message" not in conversation.nodes[1].source_node_json
    assert "mapping" not in conversation.source_conversation_json
    assert conversation.source_conversation_json["unknown_conversation_metadata"] == {"preserved": True}
    assert conversation.messages[0].content_text == "caption"
    assert isinstance(conversation.messages[0].content_json["parts"][1], dict)


def test_missing_message_id_uses_an_explicit_deterministic_node_locator():
    payload = _payload()
    del payload[0]["mapping"]["n/1~x"]["message"]["id"]

    first = tuple(ChatGPTExportParser().parse(payload))[0].messages[0]
    second = tuple(ChatGPTExportParser().parse(copy.deepcopy(payload)))[0].messages[0]

    assert first.external_message_id == "synthetic:node:n/1~x"
    assert second.external_message_id == first.external_message_id


def test_missing_and_null_message_id_remain_distinct_source_observations():
    absent_payload = _payload()
    del absent_payload[0]["mapping"]["n/1~x"]["message"]["id"]
    null_payload = _payload()
    null_payload[0]["mapping"]["n/1~x"]["message"]["id"] = None

    absent = tuple(ChatGPTExportParser().parse(absent_payload))[0].messages[0]
    null = tuple(ChatGPTExportParser().parse(null_payload))[0].messages[0]

    assert absent.external_message_id == null.external_message_id
    assert absent.observation_evidence_json["message_id_presence"] == "absent"
    assert null.observation_evidence_json["message_id_presence"] == "null"
    assert compute_source_observation_fingerprint(absent.observation_evidence_json) != compute_source_observation_fingerprint(null.observation_evidence_json)
    assert compute_canonical_fragment_fingerprint(absent.source_fragment_json, absent.raw_json_pointer) != compute_canonical_fragment_fingerprint(null.source_fragment_json, null.raw_json_pointer)


@pytest.mark.parametrize(
    ("mutate", "error_type"),
    [
        (lambda value: value[0].update(current_node="unknown"), "missing_current_node"),
        (lambda value: value[0]["mapping"]["root"].update(children=[]), "children_parent_disagreement"),
        (lambda value: value[0]["mapping"]["root"].update(parent="leaf"), "cycle"),
        (
            lambda value: value[0]["mapping"]["n/1~x"]["message"]["content"].update(content_type="future_shape"),
            "unsupported_content_type",
        ),
        (lambda value: value[0]["mapping"]["n/1~x"]["message"].update(author=[]), "invalid_author"),
    ],
)
def test_structural_variants_fail_closed(mutate, error_type):
    payload = _payload()
    mutate(payload)
    with pytest.raises(ParserValidationError) as caught:
        ChatGPTExportParser().validate(payload)
    assert any(error["type"] == error_type for error in caught.value.errors)


def test_unknown_nonstructural_metadata_is_preserved_and_observation_only():
    original = tuple(ChatGPTExportParser().parse(_payload()))[0].messages[0]
    changed_payload = _payload()
    changed_message = changed_payload[0]["mapping"]["n/1~x"]["message"]
    changed_message["metadata"]["volatile_export_field"] = "second"
    changed_message["author"]["metadata"]["volatile_author"] = 2
    changed_message["content"]["parts"][1]["metadata"]["volatile"] = 2
    changed_message["content"]["parts"][1]["expiry_datetime"] = "different"
    changed = tuple(ChatGPTExportParser().parse(changed_payload))[0].messages[0]

    assert original.semantic_evidence_json == changed.semantic_evidence_json
    assert original.observation_evidence_json != changed.observation_evidence_json
    assert compute_semantic_revision_fingerprint(original.semantic_evidence_json) == compute_semantic_revision_fingerprint(changed.semantic_evidence_json)
    assert compute_source_observation_fingerprint(original.observation_evidence_json) != compute_source_observation_fingerprint(changed.observation_evidence_json)

    changed_payload[0]["mapping"]["n/1~x"]["message"]["metadata"]["model_slug"] = "other-model"
    semantic_change = tuple(ChatGPTExportParser().parse(changed_payload))[0].messages[0]
    assert compute_semantic_revision_fingerprint(original.semantic_evidence_json) != compute_semantic_revision_fingerprint(semantic_change.semantic_evidence_json)


@pytest.mark.parametrize(
    ("metadata_marker", "expected_status", "expected_value"),
    [
        ("absent", "absent", None),
        (None, "null", None),
        ({}, "object", {}),
    ],
)
def test_message_metadata_presence_semantics_are_not_collapsed(metadata_marker, expected_status, expected_value):
    payload = _payload()
    message = payload[0]["mapping"]["n/1~x"]["message"]
    if metadata_marker == "absent":
        del message["metadata"]
    else:
        message["metadata"] = metadata_marker

    observation = tuple(ChatGPTExportParser().parse(payload))[0].messages[0].observation_evidence_json

    assert observation["metadata_presence"] == expected_status
    assert observation["metadata"] == expected_value


def test_attachment_descriptor_and_pointer_form_one_logical_asset():
    payload = _payload()
    message = payload[0]["mapping"]["n/1~x"]["message"]
    message["metadata"]["attachments"] = [{
        "id": "asset-one",
        "name": "asset.png",
        "mime_type": "image/png",
        "size": 12,
        "future_descriptor_metadata": {"preserved": True},
    }]

    parsed = tuple(ChatGPTExportParser().parse(payload))[0].messages[0]

    assert len(parsed.attachments) == 1
    attachment = parsed.attachments[0]
    assert attachment.asset_id == "asset-one"
    assert attachment.descriptor_json["future_descriptor_metadata"] == {"preserved": True}
    assert {reference["kind"] for reference in attachment.source_references} == {
        "descriptor", "asset_pointer"
    }
    assert [reference["raw_json_pointer"] for reference in attachment.source_references] == sorted(
        reference["raw_json_pointer"] for reference in attachment.source_references
    )


def test_attachment_pointer_field_name_is_exact():
    payload = _payload()
    payload[0]["mapping"]["n/1~x"]["message"]["content"] = {
        "content_type": "multimodal_text",
        "parts": [{"not_asset_pointer": "sediment://orphan"}],
    }

    parsed = tuple(ChatGPTExportParser().parse(payload))[0].messages[0]

    assert parsed.attachments == ()


def test_anonymous_attachment_stable_keys_do_not_depend_on_unrelated_array_order():
    left = _payload()
    descriptors = [{"name": "a.txt"}, {"name": "b.txt"}]
    left[0]["mapping"]["n/1~x"]["message"]["metadata"]["attachments"] = descriptors
    right = copy.deepcopy(left)
    right[0]["mapping"]["n/1~x"]["message"]["metadata"]["attachments"].reverse()

    left_keys = {item.filename: item.stable_attachment_key for item in tuple(ChatGPTExportParser().parse(left))[0].messages[0].attachments}
    right_keys = {item.filename: item.stable_attachment_key for item in tuple(ChatGPTExportParser().parse(right))[0].messages[0].attachments}

    assert left_keys == right_keys


@pytest.mark.parametrize("field", ["create_time", "update_time", "weight"])
def test_non_finite_numbers_fail_as_parser_validation_errors(field):
    payload = _payload()
    payload[0]["mapping"]["n/1~x"]["message"][field] = float("nan")

    with pytest.raises(ParserValidationError) as caught:
        ChatGPTExportParser().validate(payload)

    assert any(error["type"] == f"invalid_{field}" for error in caught.value.errors)


def test_nested_non_finite_numbers_fail_as_parser_validation_errors():
    payload = _payload()
    payload[0]["mapping"]["n/1~x"]["message"]["metadata"]["nested"] = {"value": float("inf")}

    with pytest.raises(ParserValidationError) as caught:
        ChatGPTExportParser().validate(payload)

    assert any(
        error["type"] == "invalid_non_finite_number"
        and error["raw_pointer"].endswith("/metadata/nested/value")
        for error in caught.value.errors
    )


def test_fingerprints_are_canonical_versioned_and_locator_aware():
    left = {"b": [2, 1], "a": {"value": True}}
    right = {"a": {"value": True}, "b": [2, 1]}

    assert compute_canonical_fragment_fingerprint(left, "/0/mapping/a") == compute_canonical_fragment_fingerprint(right, "/0/mapping/a")
    assert compute_canonical_fragment_fingerprint(left, "/0/mapping/a") != compute_canonical_fragment_fingerprint(left, "/0/mapping/b")
    assert compute_canonical_fragment_fingerprint(left, "/0/mapping/a", version="canonical-json-v2") != compute_canonical_fragment_fingerprint(left, "/0/mapping/a")


def test_fragment_fingerprints_hash_the_exact_rfc6901_targets():
    payload = _payload()
    conversation = tuple(ChatGPTExportParser().parse(payload))[0]
    node = conversation.nodes[1]
    message = conversation.messages[0]

    assert conversation.source_fragment_json == payload[0]
    assert node.source_fragment_json == payload[0]["mapping"]["n/1~x"]
    assert message.source_fragment_json == payload[0]["mapping"]["n/1~x"]["message"]
    assert compute_canonical_fragment_fingerprint(conversation.source_fragment_json, conversation.raw_json_pointer)
    assert compute_canonical_fragment_fingerprint(node.source_fragment_json, node.raw_json_pointer)
    assert compute_canonical_fragment_fingerprint(message.source_fragment_json, message.raw_json_pointer)


@pytest.mark.parametrize(
    ("raw", "status", "has_value"),
    [
        (None, "missing", False),
        (1.5, "parsed", True),
        ("1.5", "parsed", True),
        ("not-a-timestamp", "invalid", False),
        (True, "unsupported_type", False),
        ({"seconds": 1}, "unsupported_type", False),
        (10**100, "out_of_range", False),
    ],
)
def test_timestamp_classification(raw, status, has_value):
    normalized, actual_status = normalize_timestamp(raw)
    assert actual_status == status
    assert (normalized is not None) is has_value


def _sample_path() -> Path:
    configured = os.getenv("QINGYU_REAL_EXPORT_SAMPLE")
    if not configured:
        pytest.skip("set QINGYU_REAL_EXPORT_SAMPLE to run the private real-export acceptance")
    sample = Path(configured)
    if not sample.is_file():
        pytest.skip("configured private real-export sample is unavailable")
    if hashlib.sha256(sample.read_bytes()).hexdigest() != SAMPLE_SHA256:
        pytest.skip("configured file is not approved Sample #001")
    return sample


def test_private_sample_001_parser_and_tree_fidelity():
    sample = _sample_path()
    with zipfile.ZipFile(sample) as source:
        payload = json.loads(source.read("conversations.json"))
        member_names = source.namelist()
    assert len(member_names) == 41

    conversations = tuple(ChatGPTExportParser().parse(payload))
    totals = Counter()
    logical_asset_ids = set()
    source_asset_pointer_count = 0
    descriptor_count = 0
    content_types = Counter()
    metadata_presence = Counter()

    assert len(conversations) == len(payload) == 15
    for source_conversation, parsed in zip(payload, conversations, strict=True):
        source_mapping = source_conversation["mapping"]
        parsed_nodes = {node.node_id: node for node in parsed.nodes}
        parent = {node_id: node.parent_node_id for node_id, node in parsed_nodes.items()}
        child_counts = Counter(value for value in parent.values() if value is not None)
        source_message_ids = {
            node_id: node["message"]["id"]
            for node_id, node in source_mapping.items()
            if isinstance(node.get("message"), dict)
        }
        parsed_message_ids = {message.node_id: message.external_message_id for message in parsed.messages}

        assert set(parsed_nodes) == set(source_mapping)
        assert parent == {node_id: node.get("parent") for node_id, node in source_mapping.items()}
        assert parsed_message_ids == source_message_ids
        assert parsed.current_node_id == source_conversation["current_node"]
        assert {
            node_id: node.declared_children
            for node_id, node in parsed_nodes.items()
        } == {
            node_id: tuple(node["children"]) if "children" in node else None
            for node_id, node in source_mapping.items()
        }

        roots = {node_id for node_id, parent_id in parent.items() if parent_id is None}
        branches = {node_id for node_id, count in child_counts.items() if count > 1}
        leaves = {node_id for node_id in parent if child_counts[node_id] == 0}
        current_path = []
        cursor = parsed.current_node_id
        while cursor is not None:
            current_path.append(cursor)
            cursor = parent[cursor]

        totals.update(
            nodes=len(parsed.nodes),
            messages=len(parsed.messages),
            null_nodes=sum(node.message_presence == "null" for node in parsed.nodes),
            roots=len(roots),
            branches=len(branches),
            leaves=len(leaves),
            current_nodes=int(parsed.current_node_id is not None),
            current_path_nodes=len(current_path),
        )
        for message in parsed.messages:
            content_types[message.content_type] += 1
            metadata_presence[message.observation_evidence_json["metadata_presence"]] += 1
            for attachment in message.attachments:
                logical_asset_ids.add(attachment.asset_id)
                descriptor_count += sum(reference["kind"] == "descriptor" for reference in attachment.source_references)
                source_asset_pointer_count += sum(reference["kind"] == "asset_pointer" for reference in attachment.source_references)

    assert totals == Counter({
        "nodes": 944,
        "messages": 929,
        "null_nodes": 15,
        "roots": 15,
        "branches": 3,
        "leaves": 18,
        "current_nodes": 15,
        "current_path_nodes": 941,
    })
    assert content_types == Counter({
        "text": 620,
        "multimodal_text": 156,
        "thoughts": 77,
        "reasoning_recap": 76,
    })
    assert metadata_presence == Counter({"object": 896, "absent": 33})
    assert len(logical_asset_ids) == 9
    assert descriptor_count == 5
    assert source_asset_pointer_count == 8

    dat_member_stem_counts = Counter(
        Path(name).stem
        for name in member_names
        if name.lower().endswith(".dat")
    )
    dat_member_stems = set(dat_member_stem_counts)
    assert logical_asset_ids <= dat_member_stems
    assert all(dat_member_stem_counts[asset_id] == 1 for asset_id in logical_asset_ids)
    assert len(dat_member_stems - logical_asset_ids) == 3
