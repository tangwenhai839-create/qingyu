import json
import logging
import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime

import pytest
from app.evidence.cursor import encode_cursor
from app.evidence.search import (
    BIGRAM_INDEX,
    EvidenceSearchRejected,
    normalize_search_query,
    occurrence_snippet,
)
from app.main import app
from app.models import (
    ArchiveObject,
    Conversation,
    ConversationNode,
    ConversationObservation,
    ImportBatchCommit,
    ImportJob,
    MappingNodeObservation,
    Message,
    MessageObservation,
    MessageRevision,
    SourceAccount,
)
from fastapi.testclient import TestClient
from sqlalchemy import String, bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Session

READ_HEADERS = {"Authorization": "Bearer qingyu-test-evidence-read-token-only"}
ADMIN_HEADERS = {"Authorization": "Bearer qingyu-test-token-only"}
CURSOR_KEY = "qingyu-test-server-only-cursor-signing-key"
PROBE_ROWS = 8000
PROBE_QUERY = "运维"


def _fingerprint(character: str) -> str:
    return character * 64


def _plan_nodes(node):
    yield node
    for child in node.get("Plans") or []:
        yield from _plan_nodes(child)


def _load_plan(value):
    if isinstance(value, str):
        value = json.loads(value)
    return value[0]["Plan"]


def _explain_json(connection, sql, params, bindparams=()):
    statement = text("EXPLAIN (FORMAT JSON) " + sql)
    if bindparams:
        statement = statement.bindparams(*bindparams)
    return _load_plan(connection.execute(statement, params).scalar())


def test_normalize_search_query_and_snippet_bounds():
    assert normalize_search_query("  运维  ") == "运维"
    assert normalize_search_query("Ab") == "Ab"
    with pytest.raises(EvidenceSearchRejected) as too_short:
        normalize_search_query(" 运 ")
    assert too_short.value.code == "query_too_short"
    with pytest.raises(EvidenceSearchRejected) as nul:
        normalize_search_query("运维\x00")
    assert nul.value.code == "query_invalid"
    with pytest.raises(EvidenceSearchRejected) as too_long:
        normalize_search_query("甲" * 201)
    assert too_long.value.code == "query_too_long"
    assert occurrence_snippet("记忆库", "记忆库") == "记忆库"
    content = ("甲" * 180) + "数据库运维和运维备份" + ("乙" * 180)
    snippet = occurrence_snippet(content, "运维")
    assert "运维" in snippet
    assert snippet != content
    assert len(snippet) <= 202
    assert snippet.startswith("…") or snippet.endswith("…")


def test_evidence_query_v1_schema_is_not_extended():
    repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    schema_path = os.path.join(repo, "contracts", "schemas", "evidence-query-v1.json")
    with open(schema_path, encoding="utf-8") as handle:
        schema = handle.read()
    assert "evidence-query-v1" in schema
    assert "evidence-search-v1" not in schema
    assert "/search" not in schema


def test_bigram_function_matches_contiguous_pairs(verified_test_database):
    with verified_test_database.connect() as connection:
        pairs = connection.execute(
            text("SELECT public.qingyu_text_bigrams(:q)"),
            {"q": PROBE_QUERY},
        ).scalar_one()
        assert list(pairs) == [PROBE_QUERY]
        folded = connection.execute(
            text("SELECT public.qingyu_text_bigrams(:q)"),
            {"q": "AbC"},
        ).scalar_one()
        assert set(folded) == {"ab", "bc"}
        empty = connection.execute(
            text("SELECT public.qingyu_text_bigrams(:q)"),
            {"q": "甲"},
        ).scalar_one()
        assert list(empty) == []
        assert connection.execute(
            text("SELECT public.qingyu_text_bigrams(NULL)")
        ).scalar_one() is None
        contained = connection.execute(
            text(
                "SELECT public.qingyu_text_bigrams(:q) "
                "<@ public.qingyu_text_bigrams(:text)"
            ),
            {"q": PROBE_QUERY, "text": "数据库运维包括备份步骤"},
        ).scalar_one()
        assert contained is True
        indexdef = connection.execute(
            text("SELECT indexdef FROM pg_indexes WHERE indexname = :name"),
            {"name": BIGRAM_INDEX},
        ).scalar_one()
    assert "USING gin" in indexdef
    assert "qingyu_text_bigrams" in indexdef


def _delete_probe(connection):
    connection.execute(
        text(
            """
            DELETE FROM message_revisions
            WHERE message_id IN (
                SELECT m.id FROM messages AS m
                JOIN conversations AS c ON c.id = m.conversation_id
                JOIN source_accounts AS s ON s.id = c.source_account_id
                WHERE s.account_label LIKE 'bigram-probe-%'
            )
            """
        )
    )
    connection.execute(
        text(
            """
            DELETE FROM messages
            WHERE conversation_id IN (
                SELECT c.id FROM conversations AS c
                JOIN source_accounts AS s ON s.id = c.source_account_id
                WHERE s.account_label LIKE 'bigram-probe-%'
            )
            """
        )
    )
    connection.execute(
        text(
            """
            DELETE FROM conversations
            WHERE source_account_id IN (
                SELECT id FROM source_accounts WHERE account_label LIKE 'bigram-probe-%'
            )
            """
        )
    )
    connection.execute(
        text("DELETE FROM source_accounts WHERE account_label LIKE 'bigram-probe-%'")
    )


def _insert_probe(connection, *, rows: int):
    account_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    label = f"bigram-probe-{account_id}"
    connection.execute(
        text(
            """
            INSERT INTO source_accounts (
                id, platform, account_label, metadata, created_at, updated_at
            )
            VALUES (
                :id, 'chatgpt', :label, '{}'::jsonb, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            )
            """
        ),
        {"id": account_id, "label": label},
    )
    connection.execute(
        text(
            """
            INSERT INTO conversations (
                id, source_account_id, external_conversation_id, metadata,
                created_at, updated_at
            )
            VALUES (
                :id, :account_id, :external_id, '{}'::jsonb,
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            )
            """
        ),
        {
            "id": conversation_id,
            "account_id": account_id,
            "external_id": label,
        },
    )
    connection.execute(
        text(
            """
            INSERT INTO messages (
                id, conversation_id, external_message_id, created_at, updated_at
            )
            SELECT gen_random_uuid(), :conversation_id, 'bigram-probe-' || i,
                   CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            FROM generate_series(1, :rows) AS gs(i)
            """
        ),
        {"conversation_id": conversation_id, "rows": rows},
    )
    connection.execute(
        text(
            """
            INSERT INTO message_revisions (
                id, message_id, revision_no, role, content_text,
                revision_fingerprint, created_at
            )
            SELECT gen_random_uuid(), m.id, 1, 'user',
                CASE
                    WHEN mod((substring(m.external_message_id from '([0-9]+)$'))::int, 250) = 0
                    THEN '第' || substring(m.external_message_id from '([0-9]+)$')
                         || '条记录提到数据库运维和备份步骤'
                    ELSE '今天天气不错适合出门散步看书'
                         || substring(m.external_message_id from '([0-9]+)$')
                END,
                md5(m.id::text),
                CURRENT_TIMESTAMP
            FROM messages AS m
            WHERE m.conversation_id = :conversation_id
            """
        ),
        {"conversation_id": conversation_id},
    )


def test_two_character_chinese_query_uses_bigram_index_not_seq_scan(verified_test_database):
    """pg_trgm does not serve a 2-character Chinese query; the bigram GIN does.

    On en_US.utf8 (the postgres:16 image locale) ``show_trgm('运维')`` is not
    contained in the trigrams of a sentence that contains those characters, and
    ``ILIKE '%运维%'`` stays a sequential scan even after a trigram GIN index
    exists. The same rows use a bitmap index scan on the bigram expression.
    """
    engine = verified_test_database
    with engine.begin() as connection:
        _delete_probe(connection)
        _insert_probe(connection, rows=PROBE_ROWS)
    try:
        with engine.begin() as connection:
            connection.execute(text("ANALYZE message_revisions"))
            grams = list(
                connection.execute(
                    text("SELECT public.qingyu_text_bigrams(:q)"),
                    {"q": PROBE_QUERY},
                ).scalar_one()
            )
            plan = _explain_json(
                connection,
                """
                SELECT id FROM message_revisions
                WHERE public.qingyu_text_bigrams(content_text) @> CAST(:query_bigrams AS text[])
                  AND strpos(lower(content_text), lower(:q)) > 0
                """,
                {"query_bigrams": grams, "q": PROBE_QUERY},
                (
                    bindparam("query_bigrams", type_=ARRAY(String())),
                    bindparam("q", type_=String()),
                ),
            )
            nodes = list(_plan_nodes(plan))
            assert any(node.get("Index Name") == BIGRAM_INDEX for node in nodes), json.dumps(
                plan, ensure_ascii=False
            )
            assert not any(
                node.get("Node Type") in {"Seq Scan", "Parallel Seq Scan"}
                and node.get("Relation Name") == "message_revisions"
                for node in nodes
            ), json.dumps(plan, ensure_ascii=False)

        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                connection.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
                connection.execute(text("DROP INDEX IF EXISTS ix_message_revisions_content_trgm_probe"))
                connection.execute(
                    text(
                        "CREATE INDEX ix_message_revisions_content_trgm_probe "
                        "ON message_revisions USING gin (content_text gin_trgm_ops)"
                    )
                )
                connection.execute(text("ANALYZE message_revisions"))
                contained = connection.execute(
                    text(
                        "SELECT show_trgm(:q) <@ show_trgm(:sample)"
                    ),
                    {"q": PROBE_QUERY, "sample": "数据库运维包括备份步骤"},
                ).scalar_one()
                assert contained is False
                ilike_plan = _load_plan(
                    connection.execute(
                        text(
                            """
                            EXPLAIN (FORMAT JSON)
                            SELECT id FROM message_revisions
                            WHERE content_text ILIKE :pattern
                            """
                        ),
                        {"pattern": f"%{PROBE_QUERY}%"},
                    ).scalar()
                )
                ilike_nodes = list(_plan_nodes(ilike_plan))
                assert all(
                    node.get("Index Name") != "ix_message_revisions_content_trgm_probe"
                    for node in ilike_nodes
                ), json.dumps(ilike_plan, ensure_ascii=False)
                assert any(
                    node.get("Node Type") in {"Seq Scan", "Parallel Seq Scan"}
                    and node.get("Relation Name") == "message_revisions"
                    for node in ilike_nodes
                ), json.dumps(ilike_plan, ensure_ascii=False)
            finally:
                transaction.rollback()
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DROP INDEX IF EXISTS ix_message_revisions_content_trgm_probe")
            )
            _delete_probe(connection)
            connection.execute(text("ANALYZE message_revisions"))
            connection.execute(text("DROP EXTENSION IF EXISTS pg_trgm"))


def _add_observed_message(
    db,
    *,
    job,
    observation,
    conversation,
    order,
    external_node_id,
    role,
    content,
    timestamp,
):
    node = ConversationNode(
        id=uuid.uuid4(),
        conversation_id=conversation.id,
        external_node_id=external_node_id,
    )
    message = Message(
        id=uuid.uuid4(),
        conversation_id=conversation.id,
        external_message_id=external_node_id,
    )
    db.add_all([node, message])
    db.flush()
    revision = MessageRevision(
        id=uuid.uuid4(),
        message_id=message.id,
        revision_no=1,
        role=role,
        content_text=content,
        content_type="text",
        normalized_timestamp=timestamp,
        revision_fingerprint=uuid.uuid4().hex + uuid.uuid4().hex[:32],
    )
    db.add(revision)
    db.flush()
    mapping = MappingNodeObservation(
        id=uuid.uuid4(),
        conversation_observation_id=observation.id,
        conversation_id=conversation.id,
        conversation_node_id=node.id,
        message_id=message.id,
        message_revision_id=revision.id,
        message_presence="object",
        observation_order=order,
        raw_mapping_pointer=f"/mapping/{external_node_id}",
        declared_children_json=[],
        source_node_json={},
        source_observation_fingerprint=_fingerprint("e"),
        canonical_fragment_fingerprint=_fingerprint("f"),
        fragment_fingerprint_version="1",
    )
    message_observation = MessageObservation(
        id=uuid.uuid4(),
        import_job_id=job.id,
        message_id=message.id,
        message_revision_id=revision.id,
        external_node_id=external_node_id,
        metadata_json={},
    )
    db.add_all([mapping, message_observation])
    db.flush()
    return {
        "node_id": node.id,
        "message_id": message.id,
        "revision_id": revision.id,
        "mapping_id": mapping.id,
        "message_observation_id": message_observation.id,
    }


def _seed_chinese_evidence(db):
    archive = ArchiveObject(
        id=uuid.uuid4(),
        sha256=uuid.uuid4().hex + uuid.uuid4().hex,
        size_bytes=128,
        relative_path=f"test/search-{uuid.uuid4()}.zip",
        original_filename="search.zip",
        media_type="application/zip",
        integrity_status="verified",
        metadata_json={},
    )
    chatgpt = SourceAccount(
        id=uuid.uuid4(),
        platform="chatgpt",
        account_label=f"evidence-search-{uuid.uuid4()}",
        metadata_json={},
    )
    claude = SourceAccount(
        id=uuid.uuid4(),
        platform="claude",
        account_label=f"evidence-search-{uuid.uuid4()}",
        metadata_json={},
    )
    db.add_all([archive, chatgpt, claude])
    db.flush()
    conversation = Conversation(
        id=uuid.uuid4(),
        source_account_id=chatgpt.id,
        external_conversation_id=f"search-{uuid.uuid4()}",
        title="mutable title that search must ignore",
        metadata_json={},
    )
    other = Conversation(
        id=uuid.uuid4(),
        source_account_id=claude.id,
        external_conversation_id=f"search-{uuid.uuid4()}",
        title="other",
        metadata_json={},
    )
    job = ImportJob(
        id=uuid.uuid4(),
        source_account_id=chatgpt.id,
        archive_object_id=archive.id,
        parser_name="chatgpt-export",
        parser_version="1",
        status="completed",
        checkpoint={},
        report_json={},
    )
    other_job = ImportJob(
        id=uuid.uuid4(),
        source_account_id=claude.id,
        archive_object_id=archive.id,
        parser_name="chatgpt-export",
        parser_version="1",
        status="completed",
        checkpoint={},
        report_json={},
    )
    db.add_all([conversation, other, job, other_job])
    db.flush()
    observed = datetime(2026, 4, 1, tzinfo=UTC)
    batch = ImportBatchCommit(
        id=uuid.uuid4(),
        import_job_id=job.id,
        conversation_id=conversation.id,
        sequence_no=1,
        batch_key=f"search-{uuid.uuid4()}",
        external_conversation_id=conversation.external_conversation_id,
        parser_name="chatgpt-export",
        contract_version="1",
        report_delta={},
        counts_delta={},
        committed_at=observed,
    )
    other_batch = ImportBatchCommit(
        id=uuid.uuid4(),
        import_job_id=other_job.id,
        conversation_id=other.id,
        sequence_no=1,
        batch_key=f"search-{uuid.uuid4()}",
        external_conversation_id=other.external_conversation_id,
        parser_name="chatgpt-export",
        contract_version="1",
        report_delta={},
        counts_delta={},
        committed_at=observed,
    )
    db.add_all([batch, other_batch])
    db.flush()
    observation = ConversationObservation(
        id=uuid.uuid4(),
        import_batch_commit_id=batch.id,
        conversation_id=conversation.id,
        archive_object_id=archive.id,
        source_member="conversations.json",
        source_conversation_index=0,
        raw_conversation_pointer="/0",
        current_node_status="absent",
        source_conversation_json={"title": "数据库运维讨论"},
        create_time_parse_status="absent",
        update_time_parse_status="absent",
        time_provenance_json={},
        source_observation_fingerprint=_fingerprint("b"),
        canonical_fragment_fingerprint=_fingerprint("c"),
        fragment_fingerprint_version="1",
        observed_at=observed,
    )
    other_observation = ConversationObservation(
        id=uuid.uuid4(),
        import_batch_commit_id=other_batch.id,
        conversation_id=other.id,
        archive_object_id=archive.id,
        source_member="conversations.json",
        source_conversation_index=1,
        raw_conversation_pointer="/1",
        current_node_status="absent",
        source_conversation_json={"title": "另一来源"},
        create_time_parse_status="absent",
        update_time_parse_status="absent",
        time_provenance_json={},
        source_observation_fingerprint=_fingerprint("d"),
        canonical_fragment_fingerprint=_fingerprint("e"),
        fragment_fingerprint_version="1",
        observed_at=observed,
    )
    db.add_all([observation, other_observation])
    db.flush()
    long_content = ("甲" * 180) + "数据库运维和运维备份" + ("乙" * 180)
    undated = _add_observed_message(
        db,
        job=job,
        observation=observation,
        conversation=conversation,
        order=0,
        external_node_id="undated-ops",
        role="user",
        content=long_content,
        timestamp=None,
    )
    memory = _add_observed_message(
        db,
        job=job,
        observation=observation,
        conversation=conversation,
        order=1,
        external_node_id="memory-note",
        role="assistant",
        content="记忆库备份策略写在附录里",
        timestamp=datetime(2026, 4, 1, tzinfo=UTC),
    )
    api = _add_observed_message(
        db,
        job=job,
        observation=observation,
        conversation=conversation,
        order=2,
        external_node_id="api-note",
        role="user",
        content="API 网关备份进度100%完成",
        timestamp=datetime(2026, 5, 1, tzinfo=UTC),
    )
    other_hit = _add_observed_message(
        db,
        job=other_job,
        observation=other_observation,
        conversation=other,
        order=0,
        external_node_id="other-ops",
        role="user",
        content="另一来源的运维笔记",
        timestamp=datetime(2026, 6, 1, tzinfo=UTC),
    )
    orphan_message = Message(
        id=uuid.uuid4(),
        conversation_id=conversation.id,
        external_message_id="orphan-ops",
    )
    db.add(orphan_message)
    db.flush()
    orphan = MessageRevision(
        id=uuid.uuid4(),
        message_id=orphan_message.id,
        revision_no=1,
        role="user",
        content_text="运维孤立修订不应被检索",
        revision_fingerprint=uuid.uuid4().hex + uuid.uuid4().hex[:32],
    )
    db.add(orphan)
    db.flush()
    return {
        "archive_id": archive.id,
        "account_ids": [chatgpt.id, claude.id],
        "chatgpt_account_id": chatgpt.id,
        "conversation_id": conversation.id,
        "other_conversation_id": other.id,
        "conversation_ids": [conversation.id, other.id],
        "job_ids": [job.id, other_job.id],
        "batch_ids": [batch.id, other_batch.id],
        "observation_id": observation.id,
        "observation_ids": [observation.id, other_observation.id],
        "message_ids": [
            undated["message_id"],
            memory["message_id"],
            api["message_id"],
            other_hit["message_id"],
            orphan_message.id,
        ],
        "revision_ids": [
            undated["revision_id"],
            memory["revision_id"],
            api["revision_id"],
            other_hit["revision_id"],
            orphan.id,
        ],
        "node_ids": [
            undated["node_id"],
            memory["node_id"],
            api["node_id"],
            other_hit["node_id"],
        ],
        "undated_mapping_id": undated["mapping_id"],
        "memory_mapping_id": memory["mapping_id"],
        "api_mapping_id": api["mapping_id"],
        "other_mapping_id": other_hit["mapping_id"],
        "undated_observation_id": undated["message_observation_id"],
        "api_revision_id": api["revision_id"],
    }


def _delete_in(connection, sql, ids):
    connection.execute(
        text(sql).bindparams(bindparam("ids", expanding=True)),
        {"ids": list(ids)},
    )


def _delete_seed(engine, seeded):
    with engine.begin() as connection:
        _delete_in(
            connection,
            "DELETE FROM message_observations WHERE import_job_id IN :ids",
            seeded["job_ids"],
        )
        _delete_in(
            connection,
            "DELETE FROM mapping_node_observations WHERE conversation_observation_id IN :ids",
            seeded["observation_ids"],
        )
        _delete_in(
            connection,
            "DELETE FROM conversation_observations WHERE id IN :ids",
            seeded["observation_ids"],
        )
        _delete_in(
            connection,
            "DELETE FROM import_batch_commits WHERE id IN :ids",
            seeded["batch_ids"],
        )
        _delete_in(
            connection,
            "DELETE FROM message_revisions WHERE id IN :ids",
            seeded["revision_ids"],
        )
        _delete_in(connection, "DELETE FROM messages WHERE id IN :ids", seeded["message_ids"])
        _delete_in(
            connection,
            "DELETE FROM conversation_nodes WHERE id IN :ids",
            seeded["node_ids"],
        )
        _delete_in(connection, "DELETE FROM import_jobs WHERE id IN :ids", seeded["job_ids"])
        _delete_in(
            connection,
            "DELETE FROM conversations WHERE id IN :ids",
            seeded["conversation_ids"],
        )
        connection.execute(
            text("DELETE FROM archive_objects WHERE id = :id"),
            {"id": seeded["archive_id"]},
        )
        _delete_in(
            connection,
            "DELETE FROM source_accounts WHERE id IN :ids",
            seeded["account_ids"],
        )


def _post(client, body, headers=READ_HEADERS):
    return client.post("/api/v1/evidence/search", headers=headers, json=body)


def test_search_finds_chinese_evidence_and_keeps_evidence_query_v1(verified_test_database, caplog):
    engine = verified_test_database
    connection = engine.connect()
    transaction = connection.begin()
    db = Session(bind=connection)
    seeded = _seed_chinese_evidence(db)
    transaction.commit()
    db.close()
    connection.close()
    try:
        client = TestClient(app)
        with caplog.at_level(logging.DEBUG):
            before = engine.connect()
            revision_count = before.scalar(text("SELECT count(*) FROM message_revisions"))
            before.close()
            found = _post(
                client,
                {"q": " 运维 ", "conversation_id": str(seeded["conversation_id"])},
            )
            after = engine.connect()
            assert after.scalar(text("SELECT count(*) FROM message_revisions")) == revision_count
            after.close()
        assert found.status_code == 200, found.text
        assert found.headers["cache-control"] == "private, no-store"
        payload = found.json()
        assert payload["contract_version"] == "evidence-search-v1"
        assert payload["meaning"] == "gateway_recorded_historical_evidence"
        assert payload["consistency"] == "keyword_substring_match_v1"
        assert payload["next_cursor"] is None
        assert len(payload["items"]) == 1
        hit = payload["items"][0]
        assert hit["mapping_node_observation_id"] == str(seeded["undated_mapping_id"])
        assert hit["message_observation_id"] == str(seeded["undated_observation_id"])
        assert hit["conversation_observation_id"] == str(seeded["observation_id"])
        assert hit["source_platform"] == "chatgpt"
        assert hit["role"] == "user"
        assert hit["normalized_timestamp"] is None
        assert hit["score"] == 2
        assert hit["match_mode"] == "bigram"
        assert "运维" in hit["snippet"]
        assert hit["snippet"].startswith("…")
        assert "content_text" not in hit
        assert "运维" not in caplog.text
        for forbidden in (
            "content_text",
            "relative_path",
            "source_member",
            "raw_json_pointer",
            "source_conversation_json",
            "mutable title",
        ):
            assert forbidden not in found.text

        both = _post(client, {"q": "运维"})
        assert [item["mapping_node_observation_id"] for item in both.json()["items"]] == [
            str(seeded["other_mapping_id"]),
            str(seeded["undated_mapping_id"]),
        ]
        chatgpt_only = _post(
            client,
            {
                "q": "运维",
                "source_platform": "chatgpt",
                "source_account_id": str(seeded["chatgpt_account_id"]),
            },
        )
        assert [item["mapping_node_observation_id"] for item in chatgpt_only.json()["items"]] == [
            str(seeded["undated_mapping_id"])
        ]
        assert _post(client, {"q": "孤立"}).json()["items"] == []
        # 库 and 备 both occur, but not as the contiguous pair 备库.
        assert _post(client, {"q": "备库"}).json()["items"] == []

        memory = _post(client, {"q": "记忆库"})
        assert memory.status_code == 200
        assert [item["mapping_node_observation_id"] for item in memory.json()["items"]] == [
            str(seeded["memory_mapping_id"])
        ]
        assert memory.json()["items"][0]["role"] == "assistant"
        assert memory.json()["items"][0]["score"] == 1

        percent = _post(client, {"q": "0%"})
        assert [item["mapping_node_observation_id"] for item in percent.json()["items"]] == [
            str(seeded["api_mapping_id"])
        ]
        folded = _post(client, {"q": "ap"})
        assert [item["message_revision_id"] for item in folded.json()["items"]] == [
            str(seeded["api_revision_id"])
        ]

        page = _post(
            client,
            {"q": "备份", "conversation_id": str(seeded["conversation_id"]), "limit": 2},
        )
        assert page.status_code == 200
        first_ids = [item["mapping_node_observation_id"] for item in page.json()["items"]]
        assert first_ids == [
            str(seeded["api_mapping_id"]),
            str(seeded["memory_mapping_id"]),
        ]
        cursor = page.json()["next_cursor"]
        decoded = json.loads(_b64decode_cursor(cursor))
        assert decoded["kind"] == "evidence-search"
        assert "q" not in decoded
        assert PROBE_QUERY not in json.dumps(decoded, ensure_ascii=False)
        assert "备份" not in json.dumps(decoded, ensure_ascii=False)
        second = _post(
            client,
            {
                "q": "备份",
                "conversation_id": str(seeded["conversation_id"]),
                "limit": 2,
                "cursor": cursor,
            },
        )
        assert second.status_code == 200
        second_ids = [item["mapping_node_observation_id"] for item in second.json()["items"]]
        assert second_ids == [str(seeded["undated_mapping_id"])]
        assert second.json()["next_cursor"] is None
        assert set(first_ids).isdisjoint(second_ids)

        window = _post(
            client,
            {
                "q": "备份",
                "conversation_id": str(seeded["conversation_id"]),
                "from": "2026-05-01T00:00:00Z",
                "to": "2026-05-02T00:00:00Z",
            },
        )
        assert [item["mapping_node_observation_id"] for item in window.json()["items"]] == [
            str(seeded["api_mapping_id"])
        ]

        listed = client.get(
            "/api/v1/evidence/conversation-observations",
            headers=READ_HEADERS,
            params={"conversation_id": str(seeded["conversation_id"])},
        )
        assert listed.status_code == 200
        assert listed.json()["contract_version"] == "evidence-query-v1"
        assert listed.json()["items"][0]["observed_title"] == "数据库运维讨论"
        assert "mutable title" not in listed.text

        assert _post(client, {"q": "运维"}, headers=ADMIN_HEADERS).status_code == 200
        missing = _post(client, {"q": "运维"}, headers=None)
        assert missing.status_code == 401
        assert missing.headers["cache-control"] == "private, no-store"
        assert missing.json()["detail"]["code"] == "authentication_required"
        wrong = client.post(
            "/api/v1/evidence/search",
            headers={"Authorization": "Bearer wrong"},
            json={"q": "运维"},
        )
        assert wrong.status_code == 401
        assert client.post("/api/v1/import/chatgpt", headers=READ_HEADERS).status_code == 401
    finally:
        _delete_seed(engine, seeded)


def _b64decode_cursor(cursor: str) -> bytes:
    import base64

    payload = cursor.split(".", 1)[0]
    return base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))


def test_search_rejects_bad_queries_without_echoing_them(verified_test_database):
    client = TestClient(app)
    short = _post(client, {"q": "运"})
    assert short.status_code == 400
    assert short.headers["cache-control"] == "private, no-store"
    assert short.json() == {"detail": {"code": "query_too_short"}}
    assert "运" not in short.text
    blank = _post(client, {"q": "  "})
    assert blank.json()["detail"]["code"] == "query_too_short"
    long_query = "甲" * 201
    too_long = _post(client, {"q": long_query})
    assert too_long.status_code == 400
    assert too_long.json()["detail"]["code"] == "query_too_long"
    assert "甲" not in too_long.text
    nul = _post(client, {"q": "运维\u0000"})
    assert nul.status_code == 400
    assert nul.json()["detail"]["code"] == "query_invalid"
    assert "运维" not in nul.text
    backwards = _post(
        client,
        {"q": "运维", "from": "2026-06-01T00:00:00Z", "to": "2026-01-01T00:00:00Z"},
    )
    assert backwards.status_code == 400
    assert backwards.json()["detail"]["code"] == "query_range_invalid"
    assert "运维" not in backwards.text
    invalid = _post(client, {"q": "运维", "limit": 51, "semantic": True})
    assert invalid.status_code == 422
    assert invalid.json() == {"detail": {"code": "query_invalid"}}
    assert "运维" not in invalid.text
    assert "semantic" not in invalid.text
    wrong_field = _post(client, {"query": "运维"})
    assert wrong_field.status_code == 422
    assert wrong_field.json() == {"detail": {"code": "query_invalid"}}
    assert "运维" not in wrong_field.text
    naive = _post(client, {"q": "运维", "from": "2026-05-01T00:00:00"})
    assert naive.status_code == 422
    assert naive.json()["detail"]["code"] == "query_invalid"
    assert client.get(
        "/api/v1/evidence/conversation-observations?limit=101",
        headers=READ_HEADERS,
    ).status_code == 422


def test_search_cursor_is_bound_to_the_query_and_filters(verified_test_database):
    engine = verified_test_database
    connection = engine.connect()
    transaction = connection.begin()
    db = Session(bind=connection)
    seeded = _seed_chinese_evidence(db)
    transaction.commit()
    db.close()
    connection.close()
    try:
        client = TestClient(app)
        page = _post(
            client,
            {"q": "备份", "conversation_id": str(seeded["conversation_id"]), "limit": 1},
        )
        cursor = page.json()["next_cursor"]
        assert cursor
        swapped = _post(
            client,
            {
                "q": "运维",
                "conversation_id": str(seeded["conversation_id"]),
                "limit": 1,
                "cursor": cursor,
            },
        )
        assert swapped.status_code == 400
        assert swapped.json()["detail"]["code"] == "evidence_cursor_invalid"
        assert "运维" not in swapped.text
        other_scope = _post(
            client,
            {"q": "备份", "limit": 1, "cursor": cursor},
        )
        assert other_scope.status_code == 400
        payload, signature = cursor.split(".", 1)
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        replacement = alphabet[(alphabet.index(signature[0]) + 1) % len(alphabet)]
        tampered = f"{payload}.{replacement}{signature[1:]}"
        assert _post(client, {"q": "备份", "cursor": tampered}).status_code == 400
        foreign = encode_cursor(
            {"kind": "conversation-observations", "after": {"id": str(uuid.uuid4())}},
            CURSOR_KEY,
        )
        assert _post(client, {"q": "备份", "cursor": foreign}).status_code == 400
        forged = encode_cursor(
            {
                "kind": "evidence-search",
                "query_fingerprint": "0" * 64,
                "after": {
                    "normalized_timestamp": None,
                    "id": str(seeded["undated_mapping_id"]),
                },
            },
            "qingyu-test-evidence-read-token-only",
        )
        assert _post(client, {"q": "备份", "cursor": forged}).status_code == 400
    finally:
        _delete_seed(engine, seeded)


def _alembic(*arguments: str) -> None:
    gateway_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    completed = subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        cwd=gateway_dir,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_bigram_migration_downgrades_and_upgrades(verified_test_database):
    # Subprocess so Alembic's fileConfig does not disable the suite's loggers.
    try:
        _alembic("downgrade", "0016_real_export_evidence")
        with verified_test_database.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "0016_real_export_evidence"
            )
            assert connection.scalar(
                text("SELECT to_regprocedure('public.qingyu_text_bigrams(text)')")
            ) is None
            assert connection.scalar(
                text("SELECT 1 FROM pg_indexes WHERE indexname = :name"),
                {"name": BIGRAM_INDEX},
            ) is None
    finally:
        _alembic("upgrade", "head")
    with verified_test_database.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0018_ingest_requests"
        )
        assert list(connection.scalar(text("SELECT public.qingyu_text_bigrams('运维')"))) == [
            "运维"
        ]
        indexdef = connection.scalar(
            text("SELECT indexdef FROM pg_indexes WHERE indexname = :name"),
            {"name": BIGRAM_INDEX},
        )
        assert indexdef and "USING gin" in indexdef
