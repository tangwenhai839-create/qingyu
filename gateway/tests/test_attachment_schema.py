import importlib.util
import uuid
from pathlib import Path

import pytest
from app.models import Attachment, AttachmentObservation


def _migration():
    path = Path(__file__).parents[1] / "alembic" / "versions" / "0015_attachment_observations.py"
    spec = importlib.util.spec_from_file_location("migration_0015_attachment_observations", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _constraint_names(model):
    return {constraint.name for constraint in model.__table__.constraints}


def _index_names(model):
    return {index.name for index in model.__table__.indexes}


def test_attachment_is_stable_source_declared_identity():
    columns = Attachment.__table__.columns
    assert {
        "id",
        "message_revision_id",
        "stable_attachment_key",
        "external_attachment_id",
        "original_reference",
        "filename",
        "media_type",
        "size_bytes",
        "descriptor_json",
        "created_at",
    } == set(columns.keys())
    assert columns.stable_attachment_key.nullable is False
    assert columns.descriptor_json.nullable is False
    assert "archive_object_id" not in columns
    assert "availability_status" not in columns
    assert "uq_attachments_revision_stable_key" in _constraint_names(Attachment)
    assert "ck_attachments_stable_key_not_blank" in _constraint_names(Attachment)
    assert "ck_attachments_size_nonnegative" in _constraint_names(Attachment)
    assert "ix_attachments_message_revision_id" in _index_names(Attachment)


def test_attachment_observation_is_attempt_scoped_evidence():
    columns = AttachmentObservation.__table__.columns
    assert columns.import_job_id.nullable is False
    assert columns.attempt_number.nullable is False
    assert columns.attachment_id.nullable is False
    assert columns.availability.nullable is False
    assert columns.observed_at.nullable is False
    assert {
        "uq_attachment_observations_job_attempt_attachment",
        "ck_attachment_observations_attempt_positive",
        "ck_attachment_observations_availability",
        "ck_attachment_observations_size_nonnegative",
    } <= _constraint_names(AttachmentObservation)
    assert {
        "ix_attachment_observations_import_job_id",
        "ix_attachment_observations_attachment_id",
        "ix_attachment_observations_archive_object_id",
    } <= _index_names(AttachmentObservation)


def test_0015_legacy_backfill_identity_is_deterministic():
    migration = _migration()
    row = {
        "external_attachment_id": None,
        "original_reference": None,
        "filename": "photo.png",
        "media_type": "image/png",
        "size_bytes": 42,
        "metadata": {"member_path": "nested\\photo.png", "z": 1},
    }
    descriptor = migration._legacy_descriptor(row)
    assert descriptor["member_path"] == "nested/photo.png"
    assert migration._legacy_base_key(row, descriptor) == "member:nested/photo.png"
    assert migration._legacy_descriptor(dict(reversed(list(row.items())))) == descriptor


def test_0015_legacy_identity_matches_parser_normalization_and_rejects_unsafe_paths():
    migration = _migration()
    row = {
        "external_attachment_id": None,
        "original_reference": r" refs\photo ",
        "metadata": {"member_path": r".\nested\photo.png"},
    }
    descriptor = migration._legacy_descriptor(row)
    assert descriptor["reference"] == "refs/photo"
    assert descriptor["member_path"] == "nested/photo.png"
    assert migration._legacy_base_key(row, descriptor) == "reference:refs/photo"
    for member_path in ("../escape", "/absolute", "C:/drive", "safe/../escape"):
        with pytest.raises(RuntimeError, match="unsafe member path"):
            migration._legacy_descriptor({"metadata": {"member_path": member_path}})


def test_0015_downgrade_preserves_selected_observation_provenance():
    migration_source = (
        Path(__file__).parents[1] / "alembic" / "versions" / "0015_attachment_observations.py"
    ).read_text(encoding="utf-8")
    assert "ORDER BY attachment_id, observed_at DESC, id DESC" in migration_source
    assert "'import_job_id', latest.import_job_id::text" in migration_source
    assert "'attempt_number', latest.attempt_number" in migration_source


def test_0015_migration_provenance_never_changes_anonymous_identity():
    migration = _migration()
    original = {
        "external_attachment_id": None,
        "original_reference": None,
        "filename": "anonymous.bin",
        "media_type": "application/octet-stream",
        "size_bytes": 9,
        "metadata": {"declared": True},
    }
    after_downgrade = {
        **original,
        "metadata": {
            "declared": True,
            "import_job_id": str(uuid.uuid4()),
            "attempt_number": 2,
        },
    }
    original_descriptor = migration._legacy_descriptor(original)
    restored_descriptor = migration._legacy_descriptor(after_downgrade)
    assert restored_descriptor == original_descriptor
    assert migration._legacy_base_key(after_downgrade, restored_descriptor) == migration._legacy_base_key(
        original, original_descriptor
    )


def test_0015_revision_chain_and_supported_availability_are_frozen():
    migration = _migration()
    assert migration.revision == "0015_attachment_observations"
    assert migration.down_revision == "0014_import_batch_commits"
    assert migration._AVAILABILITIES == {
        "available",
        "missing",
        "referenced_only",
        "failed",
    }


def test_0015_legacy_provenance_is_fail_closed_when_job_or_attempt_is_ambiguous():
    migration = _migration()
    attachment_id = uuid.uuid4()
    first_job = uuid.uuid4()
    second_job = uuid.uuid4()
    with pytest.raises(RuntimeError, match="ambiguous import-job provenance"):
        migration._resolve_legacy_provenance(
            attachment_id,
            {},
            [
                {"import_job_id": first_job, "attempt_count": 1},
                {"import_job_id": second_job, "attempt_count": 1},
            ],
        )
    with pytest.raises(RuntimeError, match="ambiguous attempt provenance"):
        migration._resolve_legacy_provenance(
            attachment_id,
            {},
            [{"import_job_id": first_job, "attempt_count": 2}],
        )


def test_0015_legacy_provenance_accepts_proven_unique_or_explicit_attempt():
    migration = _migration()
    attachment_id = uuid.uuid4()
    job_id = uuid.uuid4()
    assert migration._resolve_legacy_provenance(
        attachment_id,
        {},
        [{"import_job_id": job_id, "attempt_count": 1}],
    ) == (job_id, 1)
    assert migration._resolve_legacy_provenance(
        attachment_id,
        {"import_job_id": str(job_id), "attempt_number": 2},
        [{"import_job_id": job_id, "attempt_count": 3}],
    ) == (job_id, 2)
