import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from app.evidence.query import EvidenceQueryService, observed_title
from sqlalchemy.dialects import postgresql


class _Rows:
    def __init__(self, values):
        self.values = values

    def all(self):
        return self.values

    def first(self):
        return self.values[0] if self.values else None


class _Scalars:
    def __init__(self, values):
        self.values = values

    def __iter__(self):
        return iter(self.values)


class _FakeDb:
    def __init__(self, node_rows, attachments):
        self.node_rows = node_rows
        self.scalar_sets = [attachments]

    def execute(self, _statement):
        _statement.compile(dialect=postgresql.dialect())
        return _Rows(self.node_rows)

    def scalars(self, _statement):
        _statement.compile(dialect=postgresql.dialect())
        return _Scalars(self.scalar_sets.pop(0))

class _SequenceDb:
    def __init__(self, result_sets, scalar_sets=None):
        self.result_sets = list(result_sets)
        self.scalar_sets = list(scalar_sets or [])

    def execute(self, statement):
        statement.compile(dialect=postgresql.dialect())
        return _Rows(self.result_sets.pop(0))

    def scalars(self, statement):
        statement.compile(dialect=postgresql.dialect())
        return _Scalars(self.scalar_sets.pop(0))


def test_observed_title_never_uses_a_latest_conversation_projection():
    assert observed_title({"title": "title at observation"}) == "title at observation"
    assert observed_title({"title": {"not": "a string"}}) is None
    assert observed_title(None) is None


def test_observation_query_preserves_distinct_historical_snapshots():
    now = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)
    conversation_id = uuid.uuid4()
    account_id = uuid.uuid4()

    def row(title, minute):
        timestamp = now.replace(minute=minute)
        observation = SimpleNamespace(
            id=uuid.uuid4(),
            conversation_id=conversation_id,
            archive_object_id=uuid.uuid4(),
            observed_at=timestamp,
            source_conversation_json={"title": title},
            normalized_created_at=timestamp,
            normalized_updated_at=None,
            create_time_parse_status="parsed",
            update_time_parse_status="absent",
            current_conversation_node_id=None,
            current_node_status="absent",
            source_observation_fingerprint=title[0] * 64,
            canonical_fragment_fingerprint=title[-1] * 64,
            fragment_fingerprint_version="1",
        )
        batch = SimpleNamespace(
            id=uuid.uuid4(),
            parser_name="chatgpt-export",
            contract_version="1",
            committed_at=timestamp,
        )
        job = SimpleNamespace(source_account_id=account_id)
        account = SimpleNamespace(platform="chatgpt")
        return observation, batch, job, account

    earlier = row("earlier", 4)
    later = row("later", 5)
    db = _SequenceDb(
        [
            [(later[1].committed_at, later[0].id)],
            [earlier, later],
        ]
    )

    page = EvidenceQueryService(db).list_conversation_observations(limit=10)

    assert len(page.items) == 2
    assert [item["observed_title"] for item in page.items] == ["earlier", "later"]
    assert {item["conversation_id"] for item in page.items} == {conversation_id}
    assert page.upper_bound == (later[1].committed_at, later[0].id)


def test_node_query_returns_bounded_attachment_metadata_without_internal_locations():
    now = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)
    observation_id = uuid.uuid4()
    revision_id = uuid.uuid4()
    message_id = uuid.uuid4()
    attachment_id = uuid.uuid4()
    observation = SimpleNamespace(
        id=observation_id,
        conversation_id=uuid.uuid4(),
        archive_object_id=uuid.uuid4(),
        observed_at=now,
        source_conversation_json={"title": "then"},
        normalized_created_at=now,
        normalized_updated_at=None,
        create_time_parse_status="parsed",
        update_time_parse_status="absent",
        current_conversation_node_id=None,
        current_node_status="absent",
        source_observation_fingerprint="a" * 64,
        canonical_fragment_fingerprint="b" * 64,
        fragment_fingerprint_version="1",
    )
    batch = SimpleNamespace(
        id=uuid.uuid4(),
        import_job_id=uuid.uuid4(),
        parser_name="chatgpt-export",
        contract_version="1",
        committed_at=now,
    )
    job = SimpleNamespace(id=batch.import_job_id, source_account_id=uuid.uuid4())
    account = SimpleNamespace(platform="chatgpt")
    node_observation = SimpleNamespace(
        id=uuid.uuid4(),
        parent_conversation_node_id=None,
        declared_children_json=[],
        message_id=message_id,
        message_presence="object",
        observation_order=0,
        source_observation_fingerprint="c" * 64,
        canonical_fragment_fingerprint="d" * 64,
        fragment_fingerprint_version="1",
    )
    node = SimpleNamespace(id=uuid.uuid4())
    revision = SimpleNamespace(
        id=revision_id,
        revision_no=1,
        role="user",
        content_text="private evidence content",
        content_json={"content_type": "text", "parts": ["private evidence content"]},
        content_type="text",
        model=None,
        raw_timestamp="1",
        normalized_timestamp=now,
        normalized_update_timestamp=None,
        create_time_parse_status="parsed",
        update_time_parse_status="absent",
        semantic_revision_fingerprint="e" * 64,
        fingerprint_version="1",
        revision_fingerprint="f" * 64,
    )
    message_observation = SimpleNamespace(
        id=uuid.uuid4(),
        source_observation_fingerprint="1" * 64,
        canonical_fragment_fingerprint="2" * 64,
        fragment_fingerprint_version="1",
    )
    attachment = SimpleNamespace(
        id=attachment_id,
        message_revision_id=revision_id,
        stable_attachment_key="member:/private/path/to/photo.png",
        filename="photo.png",
        media_type="image/png",
        size_bytes=12,
        original_reference="reference:must-not-leak",
    )
    db = _FakeDb(
        [(node_observation, node, revision, message_observation)],
        [attachment],
    )
    service = EvidenceQueryService(db)
    service.get_observation_context = lambda _observation_id: (
        observation,
        batch,
        job,
        account,
    )

    page = service.list_observation_nodes(observation_id=observation_id, limit=10)

    returned_attachment = page.items[0]["message"]["attachments"][0]
    assert returned_attachment == {
        "attachment_id": attachment_id,
        "filename": "photo.png",
        "media_type": "image/png",
        "size_bytes": 12,
    }
    serialized = str(page.items)
    assert "reference:must-not-leak" not in serialized
    assert "member:/private/path/to/photo.png" not in serialized
    assert page.context["observed_title"] == "then"


def test_attachment_observation_query_returns_attempts_without_locator_values():
    now = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)
    observation_id = uuid.uuid4()
    attachment_id = uuid.uuid4()
    attachment = SimpleNamespace(
        id=attachment_id,
        filename="photo.png",
        media_type="image/png",
        size_bytes=12,
        stable_attachment_key="member:/private/photo.png",
        original_reference="reference:private-photo",
    )
    observation = SimpleNamespace(id=observation_id)
    batch = SimpleNamespace(id=uuid.uuid4(), import_job_id=uuid.uuid4())
    job = SimpleNamespace(id=batch.import_job_id)
    attempts = [
        SimpleNamespace(
            id=uuid.uuid4(),
            attachment_id=attachment_id,
            attempt_number=number,
            availability=availability,
            archive_object_id=uuid.uuid4() if number == 2 else None,
            observed_filename="photo.png",
            observed_media_type="image/png",
            observed_size_bytes=12 if number == 2 else None,
            resolution_status=availability,
            canonical_fragment_fingerprint=str(number) * 64,
            fragment_fingerprint_version="1",
            observed_at=now,
            source_member="member:/must-not-leak",
            raw_json_pointer="reference:/must-not-leak",
        )
        for number, availability in ((1, "missing"), (2, "available"))
    ]
    upper = attempts[-1]
    db = _SequenceDb(
        [
            [(attachment, observation, batch, job)],
            [(upper.attempt_number, upper.observed_at, upper.id)],
        ],
        [attempts],
    )
    page = EvidenceQueryService(db).list_attachment_observations(
        attachment_id=attachment_id,
        conversation_observation_id=observation_id,
        limit=10,
    )
    assert [item["attempt_number"] for item in page.items] == [1, 2]
    serialized = str(page)
    assert "member:/" not in serialized
    assert "reference:" not in serialized
