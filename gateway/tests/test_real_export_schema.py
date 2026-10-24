import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from app.models import (
    ArchiveObject,
    Conversation,
    ConversationNode,
    ConversationObservation,
    ImportBatchCommit,
    ImportJob,
    MappingNodeObservation,
    Message,
    MessageRevision,
    SourceAccount,
)
from conftest import validated_url
from sqlalchemy import create_engine, inspect, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

REPOSITORY = Path(__file__).parents[1]
HEAD = "0016_real_export_evidence"


def _run_alembic(database_url: str, *arguments: str, check: bool = True):
    env = os.environ.copy()
    env.update(
        {
            "DATABASE_URL": database_url,
            "APP_ENV": "test",
            "API_TOKEN": "real-export-schema-test-token",
        }
    )
    return subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        cwd=REPOSITORY,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=check,
    )


@pytest.fixture(scope="module")
def real_export_database():
    database_name = f"qingyu_disposable_rex_{uuid.uuid4().hex[:12]}"
    admin_url = validated_url.set(database="postgres")
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.exec_driver_sql(f'CREATE DATABASE "{database_name}"')
    database_url = validated_url.set(database=database_name).render_as_string(hide_password=False)
    engine = None
    try:
        # Exercise the real empty-database chain and prove that upgrading an
        # existing 0015 revision neither fabricates tree evidence nor rewrites a
        # historical MessageRevision.
        _run_alembic(database_url, "upgrade", "0015_attachment_observations")
        engine = create_engine(database_url)
        legacy_ids = {name: uuid.uuid4() for name in ("archive", "account", "conversation", "message", "revision")}
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO archive_objects "
                    "(id, sha256, size_bytes, relative_path, media_type, integrity_status, metadata) "
                    "VALUES (:id, :sha, 1, 'archive/objects/legacy', 'application/json', 'verified', '{}'::jsonb)"
                ),
                {"id": legacy_ids["archive"], "sha": uuid.uuid4().hex * 2},
            )
            connection.execute(
                text(
                    "INSERT INTO source_accounts "
                    "(id, platform, account_label, metadata, created_at, updated_at) "
                    "VALUES (:id, 'legacy', :label, '{}'::jsonb, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {"id": legacy_ids["account"], "label": str(uuid.uuid4())},
            )
            connection.execute(
                text(
                    "INSERT INTO conversations "
                    "(id, source_account_id, external_conversation_id, metadata, created_at, updated_at) "
                    "VALUES (:id, :account, 'legacy-conversation', '{}'::jsonb, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {"id": legacy_ids["conversation"], "account": legacy_ids["account"]},
            )
            connection.execute(
                text(
                    "INSERT INTO messages (id, conversation_id, external_message_id, created_at, updated_at) "
                    "VALUES (:id, :conversation, 'legacy-message', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {"id": legacy_ids["message"], "conversation": legacy_ids["conversation"]},
            )
            connection.execute(
                text(
                    "INSERT INTO message_revisions "
                    "(id, message_id, revision_no, role, content_text, revision_fingerprint, created_at) "
                    "VALUES (:id, :message, 1, 'user', 'legacy', :fingerprint, CURRENT_TIMESTAMP)"
                ),
                {
                    "id": legacy_ids["revision"],
                    "message": legacy_ids["message"],
                    "fingerprint": uuid.uuid4().hex * 2,
                },
            )
        engine.dispose()
        engine = None
        _run_alembic(database_url, "upgrade", "head")
        engine = create_engine(database_url)
        with engine.connect() as connection:
            assert connection.scalar(text("select current_database()")) == database_name
            assert connection.scalar(text("select version_num from alembic_version")) == HEAD
            legacy = connection.execute(
                text(
                    "SELECT raw_create_time_json, raw_update_time_json, normalized_update_timestamp, "
                    "create_time_parse_status, update_time_parse_status, time_provenance_json, content_type, "
                    "semantic_evidence_json, semantic_revision_fingerprint, fingerprint_version "
                    "FROM message_revisions WHERE id=:id"
                ),
                {"id": legacy_ids["revision"]},
            ).one()
            assert all(value is None for value in legacy)
            assert connection.scalar(text("select count(*) from conversation_nodes")) == 0
            assert connection.scalar(text("select count(*) from conversation_observations")) == 0
            assert connection.scalar(text("select count(*) from mapping_node_observations")) == 0
        check_result = _run_alembic(database_url, "check")
        assert "No new upgrade operations detected" in check_result.stdout + check_result.stderr
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM message_revisions WHERE id=:id"), {"id": legacy_ids["revision"]})
            connection.execute(text("DELETE FROM messages WHERE id=:id"), {"id": legacy_ids["message"]})
            connection.execute(text("DELETE FROM conversations WHERE id=:id"), {"id": legacy_ids["conversation"]})
            connection.execute(text("DELETE FROM source_accounts WHERE id=:id"), {"id": legacy_ids["account"]})
            connection.execute(text("DELETE FROM archive_objects WHERE id=:id"), {"id": legacy_ids["archive"]})
        engine.dispose()
        engine = None
        _run_alembic(database_url, "downgrade", "0015_attachment_observations")
        _run_alembic(database_url, "upgrade", "head")
        engine = create_engine(database_url)
        yield engine, database_url
    finally:
        if engine is not None:
            engine.dispose()
        with admin.connect() as connection:
            connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname=:name AND pid<>pg_backend_pid()"
                ),
                {"name": database_name},
            )
            connection.exec_driver_sql(f'DROP DATABASE IF EXISTS "{database_name}"')
        admin.dispose()


@pytest.fixture()
def schema_session(real_export_database):
    engine, _ = real_export_database
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


def _evidence_graph(session: Session):
    account = SourceAccount(platform="schema-gate", account_label=str(uuid.uuid4()), metadata_json={})
    archive = ArchiveObject(
        sha256=uuid.uuid4().hex * 2,
        size_bytes=1,
        relative_path="archive/objects/test",
        media_type="application/zip",
        integrity_status="verified",
        metadata_json={},
    )
    session.add_all([account, archive])
    session.flush()
    job = ImportJob(
        source_account_id=account.id,
        archive_object_id=archive.id,
        parser_name="chatgpt-export",
        parser_version="1",
        status="running",
        report_json={},
    )
    conversation_a = Conversation(
        source_account_id=account.id,
        external_conversation_id=f"a-{uuid.uuid4()}",
        metadata_json={},
    )
    conversation_b = Conversation(
        source_account_id=account.id,
        external_conversation_id=f"b-{uuid.uuid4()}",
        metadata_json={},
    )
    session.add_all([job, conversation_a, conversation_b])
    session.flush()
    batch = ImportBatchCommit(
        import_job_id=job.id,
        conversation_id=conversation_a.id,
        sequence_no=1,
        batch_key="conversation-a",
        external_conversation_id=conversation_a.external_conversation_id,
        parser_name="chatgpt-export@1",
        contract_version="real-export-evidence-v1",
        report_delta={},
        counts_delta={},
    )
    message_a = Message(conversation_id=conversation_a.id, external_message_id=f"ma-{uuid.uuid4()}")
    message_b = Message(conversation_id=conversation_b.id, external_message_id=f"mb-{uuid.uuid4()}")
    node_a = ConversationNode(conversation_id=conversation_a.id, external_node_id=f"na-{uuid.uuid4()}")
    parent_a = ConversationNode(conversation_id=conversation_a.id, external_node_id=f"pa-{uuid.uuid4()}")
    node_b = ConversationNode(conversation_id=conversation_b.id, external_node_id=f"nb-{uuid.uuid4()}")
    session.add_all([batch, message_a, message_b, node_a, parent_a, node_b])
    session.flush()
    revision_a = MessageRevision(
        message_id=message_a.id,
        revision_no=1,
        role="user",
        revision_fingerprint=uuid.uuid4().hex * 2,
    )
    revision_b = MessageRevision(
        message_id=message_b.id,
        revision_no=1,
        role="assistant",
        revision_fingerprint=uuid.uuid4().hex * 2,
    )
    session.add_all([revision_a, revision_b])
    session.flush()
    observation = ConversationObservation(
        import_batch_commit_id=batch.id,
        conversation_id=conversation_a.id,
        archive_object_id=archive.id,
        source_member="conversations.json",
        source_conversation_index=0,
        raw_conversation_pointer="/0",
        external_current_node_id=node_a.external_node_id,
        current_conversation_node_id=node_a.id,
        current_node_status="resolved",
        source_conversation_json={},
        create_time_parse_status="missing",
        update_time_parse_status="missing",
        time_provenance_json={},
        source_observation_fingerprint="1" * 64,
        canonical_fragment_fingerprint="2" * 64,
        fragment_fingerprint_version="canonical-json-v1",
    )
    session.add(observation)
    session.flush()
    return {
        "archive": archive,
        "batch": batch,
        "conversation_a": conversation_a,
        "conversation_b": conversation_b,
        "message_a": message_a,
        "message_b": message_b,
        "revision_a": revision_a,
        "revision_b": revision_b,
        "node_a": node_a,
        "parent_a": parent_a,
        "node_b": node_b,
        "observation": observation,
    }


def _mapping_row(graph, **overrides):
    values = {
        "conversation_observation_id": graph["observation"].id,
        "conversation_id": graph["conversation_a"].id,
        "conversation_node_id": graph["node_a"].id,
        "parent_conversation_node_id": graph["parent_a"].id,
        "external_parent_node_id": graph["parent_a"].external_node_id,
        "message_id": graph["message_a"].id,
        "message_revision_id": graph["revision_a"].id,
        "message_presence": "object",
        "observation_order": 0,
        "raw_mapping_pointer": "/0/mapping/node",
        "declared_children_json": [],
        "source_node_json": {},
        "source_observation_fingerprint": "3" * 64,
        "canonical_fragment_fingerprint": "4" * 64,
        "fragment_fingerprint_version": "canonical-json-v1",
    }
    values.update(overrides)
    return MappingNodeObservation(**values)


def test_0016_schema_matches_models_and_remains_additive(real_export_database):
    engine, _ = real_export_database
    inspector = inspect(engine)
    assert {"conversation_nodes", "conversation_observations", "mapping_node_observations"} <= set(
        inspector.get_table_names()
    )
    revision_columns = {column["name"]: column for column in inspector.get_columns("message_revisions")}
    for name in (
        "raw_create_time_json",
        "raw_update_time_json",
        "normalized_update_timestamp",
        "create_time_parse_status",
        "update_time_parse_status",
        "time_provenance_json",
        "content_type",
        "semantic_evidence_json",
        "semantic_revision_fingerprint",
        "fingerprint_version",
    ):
        assert revision_columns[name]["nullable"] is True
    with engine.connect() as connection:
        assert connection.scalar(text("select count(*) from conversation_nodes")) == 0
        assert connection.scalar(text("select count(*) from conversation_observations")) == 0
        assert connection.scalar(text("select count(*) from mapping_node_observations")) == 0


@pytest.mark.parametrize(
    "invalid_link",
    ["snapshot", "node", "parent", "message", "revision"],
)
def test_database_rejects_cross_conversation_mapping_links(schema_session, invalid_link):
    graph = _evidence_graph(schema_session)
    if invalid_link == "snapshot":
        row = _mapping_row(
            graph,
            conversation_id=graph["conversation_b"].id,
            conversation_node_id=graph["node_b"].id,
            parent_conversation_node_id=None,
            external_parent_node_id=None,
            message_id=graph["message_b"].id,
            message_revision_id=graph["revision_b"].id,
        )
    elif invalid_link == "node":
        row = _mapping_row(graph, conversation_node_id=graph["node_b"].id)
    elif invalid_link == "parent":
        row = _mapping_row(graph, parent_conversation_node_id=graph["node_b"].id)
    elif invalid_link == "message":
        row = _mapping_row(
            graph,
            message_id=graph["message_b"].id,
            message_revision_id=graph["revision_b"].id,
        )
    else:
        row = _mapping_row(graph, message_revision_id=graph["revision_b"].id)
    with pytest.raises(IntegrityError), schema_session.begin_nested():
        schema_session.add(row)
        schema_session.flush()


def test_database_rejects_cross_conversation_current_node(schema_session):
    graph = _evidence_graph(schema_session)
    second_batch = ImportBatchCommit(
        import_job_id=graph["batch"].import_job_id,
        conversation_id=graph["conversation_a"].id,
        sequence_no=2,
        batch_key="bad-current-node",
        external_conversation_id=graph["conversation_a"].external_conversation_id,
        parser_name="chatgpt-export@1",
        contract_version="real-export-evidence-v1",
        report_delta={},
        counts_delta={},
    )
    schema_session.add(second_batch)
    schema_session.flush()
    bad = ConversationObservation(
        import_batch_commit_id=second_batch.id,
        conversation_id=graph["conversation_a"].id,
        archive_object_id=graph["archive"].id,
        source_member="conversations.json",
        source_conversation_index=1,
        raw_conversation_pointer="/1",
        external_current_node_id=graph["node_b"].external_node_id,
        current_conversation_node_id=graph["node_b"].id,
        current_node_status="resolved",
        source_conversation_json={},
        create_time_parse_status="missing",
        update_time_parse_status="missing",
        time_provenance_json={},
        source_observation_fingerprint="5" * 64,
        canonical_fragment_fingerprint="6" * 64,
        fragment_fingerprint_version="canonical-json-v1",
    )
    with pytest.raises(IntegrityError), schema_session.begin_nested():
        schema_session.add(bad)
        schema_session.flush()


def test_database_rejects_parent_external_id_entity_mismatch(schema_session):
    graph = _evidence_graph(schema_session)
    row = _mapping_row(
        graph,
        external_parent_node_id=graph["node_a"].external_node_id,
        parent_conversation_node_id=graph["parent_a"].id,
    )
    with pytest.raises(IntegrityError), schema_session.begin_nested():
        schema_session.add(row)
        schema_session.flush()


def test_database_rejects_parent_entity_with_null_external_id(schema_session):
    graph = _evidence_graph(schema_session)
    row = _mapping_row(graph, external_parent_node_id=None)
    with pytest.raises(IntegrityError), schema_session.begin_nested():
        schema_session.add(row)
        schema_session.flush()


def test_database_rejects_current_external_id_entity_mismatch(schema_session):
    graph = _evidence_graph(schema_session)
    with pytest.raises(IntegrityError), schema_session.begin_nested():
        schema_session.execute(
            update(ConversationObservation)
            .where(ConversationObservation.id == graph["observation"].id)
            .values(external_current_node_id=graph["parent_a"].external_node_id)
        )
        schema_session.flush()


def test_message_presence_binding_is_database_enforced(schema_session):
    graph = _evidence_graph(schema_session)
    structural = _mapping_row(
        graph,
        message_presence="null",
        message_id=None,
        message_revision_id=None,
    )
    schema_session.add(structural)
    schema_session.flush()
    assert structural.id is not None

    invalid_object = _mapping_row(
        graph,
        observation_order=1,
        message_presence="object",
        message_id=None,
        message_revision_id=None,
    )
    with pytest.raises(IntegrityError), schema_session.begin_nested():
        schema_session.add(invalid_object)
        schema_session.flush()


def test_0016_downgrade_fails_closed_when_evidence_exists(real_export_database):
    engine, database_url = real_export_database
    with Session(engine) as session:
        graph = _evidence_graph(session)
        session.add(_mapping_row(graph))
        revision_id = graph["revision_a"].id
        session.commit()
    result = _run_alembic(
        database_url,
        "downgrade",
        "0015_attachment_observations",
        check=False,
    )
    assert result.returncode != 0
    assert "real_export_evidence_downgrade_requires_empty_evidence" in result.stdout + result.stderr
    with engine.begin() as connection:
        assert connection.scalar(text("select version_num from alembic_version")) == HEAD
        connection.execute(text("DELETE FROM mapping_node_observations"))
        connection.execute(text("DELETE FROM conversation_observations"))
        connection.execute(text("DELETE FROM conversation_nodes"))
        connection.execute(
            text(
                "UPDATE message_revisions SET semantic_revision_fingerprint=:fingerprint "
                "WHERE id=:id"
            ),
            {"id": revision_id, "fingerprint": "a" * 64},
        )

    extension_result = _run_alembic(
        database_url,
        "downgrade",
        "0015_attachment_observations",
        check=False,
    )
    assert extension_result.returncode != 0
    assert "real_export_evidence_downgrade_requires_empty_evidence" in (
        extension_result.stdout + extension_result.stderr
    )
    with engine.connect() as connection:
        assert connection.scalar(text("select version_num from alembic_version")) == HEAD
