"""separate stable attachments from attempt-scoped observations

Revision ID: 0015_attachment_observations
Revises: 0014_import_batch_commits
"""
from __future__ import annotations

import hashlib
import json
import uuid
from collections import defaultdict
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0015_attachment_observations"
down_revision = "0014_import_batch_commits"
branch_labels = None
depends_on = None


_OBSERVATION_NAMESPACE = uuid.UUID("37983d47-76e6-4bc8-9adb-c402643bead4")
_AVAILABILITIES = {"available", "missing", "referenced_only", "failed"}


def _clean_text(value: Any, *, path: bool = False) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if path:
        cleaned = cleaned.replace("\\", "/")
    return cleaned or None


def _normalize_reference(value: Any) -> str | None:
    cleaned = _clean_text(value)
    return cleaned.replace("\\", "/") if cleaned is not None else None


def _normalize_member_path(value: Any) -> str | None:
    cleaned = _clean_text(value, path=True)
    if cleaned is None:
        return None
    if cleaned.startswith("/") or ":" in cleaned:
        raise RuntimeError("legacy attachment has an unsafe member path")
    parts = []
    for part in cleaned.split("/"):
        if not part or part == ".":
            continue
        if part == "..":
            raise RuntimeError("legacy attachment has an unsafe member path")
        parts.append(part)
    if not parts:
        raise RuntimeError("legacy attachment has an unsafe member path")
    return "/".join(parts)


def _legacy_descriptor(row: dict[str, Any]) -> dict[str, Any]:
    """Preserve every source-declared legacy field in a deterministic descriptor."""
    metadata = row.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    source_metadata = {
        key: value
        for key, value in metadata.items()
        if key not in {"import_job_id", "attempt_number"}
    }
    descriptor = {
        "id": _clean_text(row.get("external_attachment_id")),
        "reference": _normalize_reference(row.get("original_reference")),
        "filename": row.get("filename"),
        "media_type": row.get("media_type"),
        "size_bytes": row.get("size_bytes"),
        "metadata": source_metadata,
    }
    member_path = _normalize_member_path(
        metadata.get("member_path")
        or metadata.get("attachment_member_path")
        or metadata.get("zip_member")
    )
    if member_path is not None:
        descriptor["member_path"] = member_path
    return descriptor


def _legacy_base_key(row: dict[str, Any], descriptor: dict[str, Any]) -> str:
    external_id = _clean_text(row.get("external_attachment_id"))
    if external_id is not None:
        return f"external:{external_id}"
    reference = _normalize_reference(row.get("original_reference"))
    if reference is not None:
        return f"reference:{reference}"
    member_path = _normalize_member_path(descriptor.get("member_path"))
    if member_path is not None:
        return f"member:{member_path}"
    canonical = json.dumps(descriptor, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"descriptor:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _observation_metadata(metadata: Any) -> dict[str, Any]:
    return {
        "migration": "0015_attachment_observations",
        "legacy_attachment_metadata": metadata if isinstance(metadata, dict) else {},
    }


def _resolve_legacy_provenance(
    attachment_id: uuid.UUID,
    metadata: Any,
    candidates: list[dict[str, Any]],
) -> tuple[uuid.UUID, int]:
    """Resolve provenance only when the legacy evidence proves one answer."""
    metadata = metadata if isinstance(metadata, dict) else {}
    explicit_job = metadata.get("import_job_id")
    explicit_attempt = metadata.get("attempt_number")
    if (explicit_job is None) != (explicit_attempt is None):
        raise RuntimeError(
            f"legacy attachment {attachment_id} has incomplete explicit provenance"
        )
    by_job = {str(candidate["import_job_id"]): candidate for candidate in candidates}
    if explicit_job is not None:
        candidate = by_job.get(str(explicit_job))
        if candidate is None:
            raise RuntimeError(
                f"legacy attachment {attachment_id} names an import job that did not observe its revision"
            )
        if not isinstance(explicit_attempt, int) or isinstance(explicit_attempt, bool) or explicit_attempt <= 0:
            raise RuntimeError(
                f"legacy attachment {attachment_id} has an invalid explicit attempt number"
            )
        maximum_attempt = max(int(candidate["attempt_count"]), 1)
        if explicit_attempt > maximum_attempt:
            raise RuntimeError(
                f"legacy attachment {attachment_id} names an attempt beyond its import job history"
            )
        return candidate["import_job_id"], explicit_attempt

    if len(candidates) != 1:
        raise RuntimeError(
            f"legacy attachment {attachment_id} has ambiguous import-job provenance"
        )
    candidate = candidates[0]
    if int(candidate["attempt_count"]) > 1:
        raise RuntimeError(
            f"legacy attachment {attachment_id} has ambiguous attempt provenance"
        )
    return candidate["import_job_id"], 1


def upgrade() -> None:
    op.add_column("attachments", sa.Column("stable_attachment_key", sa.Text(), nullable=True))
    op.add_column(
        "attachments",
        sa.Column("descriptor_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_table(
        "attachment_observations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("import_job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("attachment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("availability", sa.String(length=32), nullable=False),
        sa.Column("archive_object_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source_member", sa.Text(), nullable=True),
        sa.Column("attachment_member_path", sa.Text(), nullable=True),
        sa.Column("raw_json_pointer", sa.Text(), nullable=True),
        sa.Column("observed_filename", sa.Text(), nullable=True),
        sa.Column("observed_media_type", sa.String(length=255), nullable=True),
        sa.Column("observed_size_bytes", sa.Integer(), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "observed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint("attempt_number > 0", name="ck_attachment_observations_attempt_positive"),
        sa.CheckConstraint(
            "availability IN ('available','missing','referenced_only','failed')",
            name="ck_attachment_observations_availability",
        ),
        sa.CheckConstraint(
            "observed_size_bytes IS NULL OR observed_size_bytes >= 0",
            name="ck_attachment_observations_size_nonnegative",
        ),
        sa.ForeignKeyConstraint(["archive_object_id"], ["archive_objects.id"]),
        sa.ForeignKeyConstraint(["attachment_id"], ["attachments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["import_job_id"], ["import_jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "import_job_id",
            "attempt_number",
            "attachment_id",
            name="uq_attachment_observations_job_attempt_attachment",
        ),
    )
    op.create_index(
        "ix_attachment_observations_import_job_id",
        "attachment_observations",
        ["import_job_id"],
        unique=False,
    )
    op.create_index(
        "ix_attachment_observations_attachment_id",
        "attachment_observations",
        ["attachment_id"],
        unique=False,
    )
    op.create_index(
        "ix_attachment_observations_archive_object_id",
        "attachment_observations",
        ["archive_object_id"],
        unique=False,
    )

    connection = op.get_bind()
    legacy_rows = list(
        connection.execute(
            sa.text(
                """
                SELECT id, message_revision_id, archive_object_id,
                       external_attachment_id, original_reference, filename,
                       media_type, size_bytes, availability_status, metadata, created_at
                FROM attachments
                ORDER BY message_revision_id, id
                """
            )
        ).mappings()
    )
    occurrences: dict[tuple[uuid.UUID, str], int] = defaultdict(int)
    for raw_row in legacy_rows:
        row = dict(raw_row)
        availability = row["availability_status"]
        if availability not in _AVAILABILITIES:
            raise RuntimeError(
                f"attachment {row['id']} has unsupported legacy availability {availability!r}"
            )
        candidates = list(connection.execute(
            sa.text(
                """
                SELECT DISTINCT mo.import_job_id, ij.attempt_count
                FROM message_observations AS mo
                JOIN import_jobs AS ij ON ij.id = mo.import_job_id
                WHERE mo.message_revision_id = :revision_id
                ORDER BY mo.import_job_id
                """
            ),
            {"revision_id": row["message_revision_id"]},
        ).mappings())
        import_job_id, attempt_number = _resolve_legacy_provenance(
            row["id"], row.get("metadata"), [dict(candidate) for candidate in candidates]
        )

        descriptor = _legacy_descriptor(row)
        base_key = _legacy_base_key(row, descriptor)
        occurrence_key = (row["message_revision_id"], base_key)
        occurrences[occurrence_key] += 1
        occurrence = occurrences[occurrence_key]
        if base_key.startswith("descriptor:"):
            stable_key = f"{base_key}:occurrence:{occurrence}"
        elif occurrence == 1:
            stable_key = base_key
        else:
            # Pre-0015 allowed duplicate declared identities. Preserve each row
            # without weakening the new uniqueness contract.
            stable_key = f"{base_key}:legacy-occurrence:{occurrence}"

        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        connection.execute(
            sa.text(
                """
                UPDATE attachments
                SET stable_attachment_key = :stable_key,
                    descriptor_json = CAST(:descriptor_json AS jsonb)
                WHERE id = :attachment_id
                """
            ),
            {
                "stable_key": stable_key,
                "descriptor_json": json.dumps(descriptor, ensure_ascii=False, sort_keys=True),
                "attachment_id": row["id"],
            },
        )
        connection.execute(
            sa.text(
                """
                INSERT INTO attachment_observations (
                    id, import_job_id, attempt_number, attachment_id, availability,
                    archive_object_id, source_member, attachment_member_path,
                    raw_json_pointer, observed_filename, observed_media_type,
                    observed_size_bytes, metadata, observed_at
                ) VALUES (
                    :id, :import_job_id, :attempt_number, :attachment_id, :availability,
                    :archive_object_id, :source_member, :attachment_member_path,
                    :raw_json_pointer, :observed_filename, :observed_media_type,
                    :observed_size_bytes, CAST(:metadata AS jsonb), COALESCE(:observed_at, CURRENT_TIMESTAMP)
                )
                """
            ),
            {
                "id": uuid.uuid5(_OBSERVATION_NAMESPACE, str(row["id"])),
                "import_job_id": import_job_id,
                "attempt_number": attempt_number,
                "attachment_id": row["id"],
                "availability": availability,
                "archive_object_id": row["archive_object_id"],
                "source_member": metadata.get("source_member") or metadata.get("zip_member"),
                "attachment_member_path": metadata.get("attachment_member_path") or metadata.get("member_path"),
                "raw_json_pointer": metadata.get("raw_json_pointer") or metadata.get("raw_descriptor_pointer"),
                "observed_filename": row["filename"],
                "observed_media_type": row["media_type"],
                "observed_size_bytes": row["size_bytes"],
                "metadata": json.dumps(_observation_metadata(row.get("metadata")), ensure_ascii=False, sort_keys=True),
                "observed_at": row["created_at"],
            },
        )

    op.alter_column("attachments", "stable_attachment_key", nullable=False)
    op.alter_column("attachments", "descriptor_json", nullable=False)
    op.create_check_constraint(
        "ck_attachments_stable_key_not_blank",
        "attachments",
        "length(btrim(stable_attachment_key)) > 0",
    )
    op.create_check_constraint(
        "ck_attachments_size_nonnegative",
        "attachments",
        "size_bytes IS NULL OR size_bytes >= 0",
    )
    op.create_unique_constraint(
        "uq_attachments_revision_stable_key",
        "attachments",
        ["message_revision_id", "stable_attachment_key"],
    )
    op.create_index(
        "ix_attachments_message_revision_id",
        "attachments",
        ["message_revision_id"],
        unique=False,
    )
    op.drop_constraint("attachments_archive_object_id_fkey", "attachments", type_="foreignkey")
    op.drop_column("attachments", "archive_object_id")
    op.drop_column("attachments", "availability_status")
    op.drop_column("attachments", "metadata")


def downgrade() -> None:
    op.add_column(
        "attachments",
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column("attachments", sa.Column("availability_status", sa.String(length=32), nullable=True))
    op.add_column(
        "attachments",
        sa.Column("archive_object_id", postgresql.UUID(as_uuid=True), nullable=True),
    )

    connection = op.get_bind()
    connection.execute(
        sa.text(
            """
            WITH latest AS (
                SELECT DISTINCT ON (attachment_id)
                       attachment_id, import_job_id, attempt_number,
                       archive_object_id, availability
                FROM attachment_observations
                ORDER BY attachment_id, observed_at DESC, id DESC
            )
            UPDATE attachments AS a
            SET archive_object_id = latest.archive_object_id,
                availability_status = COALESCE(latest.availability, 'referenced_only'),
                metadata = COALESCE(a.descriptor_json->'metadata', '{}'::jsonb)
                    || jsonb_build_object(
                        'import_job_id', latest.import_job_id::text,
                        'attempt_number', latest.attempt_number
                    )
            FROM latest
            WHERE latest.attachment_id = a.id
            """
        )
    )
    connection.execute(
        sa.text(
            """
            UPDATE attachments
            SET availability_status = COALESCE(availability_status, 'referenced_only'),
                metadata = COALESCE(metadata, descriptor_json->'metadata', '{}'::jsonb)
            """
        )
    )
    op.alter_column("attachments", "availability_status", nullable=False)
    op.alter_column("attachments", "metadata", nullable=False)
    op.create_foreign_key(
        "attachments_archive_object_id_fkey",
        "attachments",
        "archive_objects",
        ["archive_object_id"],
        ["id"],
    )

    op.drop_index("ix_attachment_observations_archive_object_id", table_name="attachment_observations")
    op.drop_index("ix_attachment_observations_attachment_id", table_name="attachment_observations")
    op.drop_index("ix_attachment_observations_import_job_id", table_name="attachment_observations")
    op.drop_table("attachment_observations")
    op.drop_index("ix_attachments_message_revision_id", table_name="attachments")
    op.drop_constraint("uq_attachments_revision_stable_key", "attachments", type_="unique")
    op.drop_constraint("ck_attachments_size_nonnegative", "attachments", type_="check")
    op.drop_constraint("ck_attachments_stable_key_not_blank", "attachments", type_="check")
    op.drop_column("attachments", "descriptor_json")
    op.drop_column("attachments", "stable_attachment_key")
