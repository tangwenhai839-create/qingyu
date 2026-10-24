"""Build export bundles from committed evidence without writing.

Export opens a read-only transaction and projects the same public evidence
shapes as the Evidence Query API. It does not insert, update, or delete
evidence, archive bytes, or manifests.
"""

from __future__ import annotations

import os
import re
import stat
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import event, select, text
from sqlalchemy.orm import Session

from ..evidence.query import EvidenceQueryService
from ..evidence.schemas import (
    AttachmentAttemptEvidence,
    AttachmentEvidence,
    ConversationObservationSummary,
    NodeEvidence,
)
from ..models import ArchiveObject, Conversation
from .bundle import BundleInput, ExportBundleError, build_bundle, canonical_json

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PAGE_SIZE = 100


class ConversationNotFound(LookupError):
    def __init__(self):
        super().__init__("conversation_not_found")


class ExportIntegrityError(RuntimeError):
    def __init__(self):
        super().__init__("export_integrity_failure")


class ExportWriteForbidden(RuntimeError):
    def __init__(self):
        super().__init__("export_write_forbidden")


@dataclass(frozen=True)
class ExportBundle:
    path: Path
    manifest_sha256: str
    scope: str
    conversation_id: str | None

    def unlink(self) -> None:
        self.path.unlink(missing_ok=True)


def _public(model, value: dict) -> dict:
    return model.model_validate(value).model_dump(mode="json")


def _evidence_bytes(payload: dict) -> bytes:
    return canonical_json(payload)


def _document(items: list[dict], **extra) -> bytes:
    body = {
        "contract_version": "evidence-query-v1",
        "meaning": "gateway_recorded_historical_evidence",
        "consistency": "bounded_high_water_enumeration_v1",
        **extra,
        "items": items,
    }
    return _evidence_bytes(body)


def collect_bounded_observations(query, *, conversation_id, page_size: int = _PAGE_SIZE):
    """Page a conversation-observation enumeration without moving its high water."""
    items = []
    upper = None
    after = None
    while True:
        page = query.list_conversation_observations(
            limit=page_size,
            conversation_id=conversation_id,
            upper_bound=upper,
            after=after,
        )
        if upper is None:
            upper = page.upper_bound
        if page.has_more and not page.items:
            raise ExportIntegrityError()
        items.extend(page.items)
        if not page.has_more:
            break
        last = page.items[-1]
        new_after = (last["committed_at"], last["conversation_observation_id"])
        if new_after == after:
            raise ExportIntegrityError()
        after = new_after
    return items, upper


def collect_nodes(query, *, observation_id, page_size: int = _PAGE_SIZE):
    items = []
    context = None
    after = None
    while True:
        page = query.list_observation_nodes(
            observation_id=observation_id,
            limit=page_size,
            after=after,
        )
        if page is None:
            raise ExportIntegrityError()
        if context is None:
            context = page.context
        if page.has_more and not page.items:
            raise ExportIntegrityError()
        items.extend(page.items)
        if not page.has_more:
            break
        last = page.items[-1]
        new_after = (last["observation_order"], last["mapping_node_observation_id"])
        if new_after == after:
            raise ExportIntegrityError()
        after = new_after
    if context is None:
        raise ExportIntegrityError()
    return context, items


def collect_attachment_attempts(query, *, attachment_id, observation_id, page_size: int = _PAGE_SIZE):
    attachment = None
    items = []
    upper = None
    after = None
    while True:
        page = query.list_attachment_observations(
            attachment_id=attachment_id,
            conversation_observation_id=observation_id,
            limit=page_size,
            upper_bound=upper,
            after=after,
        )
        if page is None:
            raise ExportIntegrityError()
        if attachment is None:
            attachment = page.attachment
        if upper is None:
            upper = page.upper_bound
        if page.has_more and not page.items:
            raise ExportIntegrityError()
        items.extend(page.items)
        if not page.has_more:
            break
        last = page.items[-1]
        new_after = (
            last["attempt_number"],
            last["observed_at"],
            last["attachment_observation_id"],
        )
        if new_after == after:
            raise ExportIntegrityError()
        after = new_after
    if attachment is None:
        raise ExportIntegrityError()
    return attachment, items


def _high_water(items: list[dict], upper) -> dict | None:
    if upper is None:
        return None
    upper_id = str(upper[1])
    for item in items:
        if item["conversation_observation_id"] == upper_id:
            return {
                "committed_at": item["committed_at"],
                "conversation_observation_id": upper_id,
            }
    raise ExportIntegrityError()


def _object_record(record: ArchiveObject) -> dict:
    if not _SHA256.fullmatch(record.sha256 or ""):
        raise ExportIntegrityError()
    if record.integrity_status != "verified":
        raise ExportIntegrityError()
    if record.size_bytes is None or record.size_bytes < 0:
        raise ExportIntegrityError()
    return {
        "object_id": str(record.id),
        "sha256": record.sha256,
        "size_bytes": record.size_bytes,
        "media_type": record.media_type,
    }


def _object_file(root: Path, record: ArchiveObject) -> Path:
    expected = Path("archive") / "objects" / record.sha256[:2] / record.sha256
    relative = Path(record.relative_path)
    if relative.is_absolute() or relative.as_posix() != expected.as_posix():
        raise ExportIntegrityError()
    root_resolved = root.resolve()
    objects_root = root_resolved / "archive" / "objects"
    candidate = root / expected
    if candidate.is_symlink() or not candidate.is_file():
        raise ExportIntegrityError()
    try:
        details = candidate.lstat()
    except OSError as exc:
        raise ExportIntegrityError() from exc
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
        raise ExportIntegrityError()
    resolved = candidate.resolve()
    try:
        resolved.relative_to(objects_root)
    except ValueError as exc:
        raise ExportIntegrityError() from exc
    return resolved


class ExportService:
    def __init__(self, db: Session, archive_root: Path, query: EvidenceQueryService | None = None):
        self.db = db
        self.root = archive_root
        self.query = query or EvidenceQueryService(db)

    def export_conversation(
        self,
        conversation_id: uuid.UUID,
        *,
        generated_at: datetime | None = None,
    ) -> ExportBundle:
        self._begin_read_only()
        if self.db.get(Conversation, conversation_id) is None:
            raise ConversationNotFound()
        return self._build(
            scope="conversation",
            conversation_id=conversation_id,
            generated_at=generated_at,
            include_object_bytes=False,
        )

    def export_archive(self, *, generated_at: datetime | None = None) -> ExportBundle:
        self._begin_read_only()
        return self._build(
            scope="archive",
            conversation_id=None,
            generated_at=generated_at,
            include_object_bytes=True,
        )

    def _begin_read_only(self) -> None:
        """First statement of this transaction. A second export on the same transaction fails closed."""
        if not self.db.info.get("qingyu_export_write_guard"):
            def reject_orm_write(state) -> None:
                if state.is_insert or state.is_update or state.is_delete:
                    raise ExportWriteForbidden()

            event.listen(self.db, "do_orm_execute", reject_orm_write)
            self.db.info["qingyu_export_write_guard"] = True
        self.db.execute(text("SET TRANSACTION READ ONLY"))
        mode = self.db.execute(text("SHOW transaction_read_only")).scalar()
        if str(mode).strip() != "on":
            raise ExportWriteForbidden()

    def _build(
        self,
        *,
        scope: str,
        conversation_id: uuid.UUID | None,
        generated_at: datetime | None,
        include_object_bytes: bool,
    ) -> ExportBundle:
        raw_items, upper = collect_bounded_observations(
            self.query,
            conversation_id=conversation_id,
        )
        summaries = [_public(ConversationObservationSummary, item) for item in raw_items]
        files: list[BundleInput] = [
            BundleInput(
                path="evidence/conversation-observations.json",
                data=_document(summaries),
            )
        ]
        referenced: set[uuid.UUID] = set()
        for summary in summaries:
            observation_id = uuid.UUID(summary["conversation_observation_id"])
            referenced.add(uuid.UUID(summary["archive_object_id"]))
            context, nodes = collect_nodes(self.query, observation_id=observation_id)
            public_context = _public(ConversationObservationSummary, context)
            public_nodes = [_public(NodeEvidence, node) for node in nodes]
            prefix = f"evidence/observations/{observation_id}"
            files.append(
                BundleInput(
                    path=f"{prefix}/nodes.json",
                    data=_document(public_nodes, observation=public_context),
                )
            )
            seen_attachments: set[str] = set()
            for node in public_nodes:
                message = node.get("message")
                if not message:
                    continue
                for attachment in message.get("attachments") or []:
                    attachment_id = attachment["attachment_id"]
                    if attachment_id in seen_attachments:
                        continue
                    seen_attachments.add(attachment_id)
                    raw_attachment, attempts = collect_attachment_attempts(
                        self.query,
                        attachment_id=uuid.UUID(attachment_id),
                        observation_id=observation_id,
                    )
                    public_attachment = _public(AttachmentEvidence, raw_attachment)
                    public_attempts = [
                        _public(AttachmentAttemptEvidence, attempt) for attempt in attempts
                    ]
                    for attempt in public_attempts:
                        if attempt["archive_object_id"]:
                            referenced.add(uuid.UUID(attempt["archive_object_id"]))
                    files.append(
                        BundleInput(
                            path=f"{prefix}/attachments/{attachment_id}.json",
                            data=_document(public_attempts, attachment=public_attachment),
                        )
                    )

        if include_object_bytes:
            records = list(
                self.db.scalars(select(ArchiveObject).order_by(ArchiveObject.sha256, ArchiveObject.id))
            )
            seen_digests: set[str] = set()
            exported_ids: set[uuid.UUID] = set()
            for record in records:
                public = _object_record(record)
                if public["sha256"] in seen_digests:
                    raise ExportIntegrityError()
                seen_digests.add(public["sha256"])
                exported_ids.add(record.id)
                source = _object_file(self.root, record)
                files.append(
                    BundleInput(
                        path=f"objects/{public['sha256']}.json",
                        data=_evidence_bytes(public),
                    )
                )
                files.append(
                    BundleInput(
                        path=f"objects/{public['sha256']}",
                        source=source,
                        expected_sha256=public["sha256"],
                        expected_size=public["size_bytes"],
                    )
                )
            if not referenced <= exported_ids:
                raise ExportIntegrityError()
        else:
            records = []
            if referenced:
                records = list(
                    self.db.scalars(select(ArchiveObject).where(ArchiveObject.id.in_(referenced)))
                )
            found = {record.id for record in records}
            if found != referenced:
                raise ExportIntegrityError()
            public_records = sorted(
                (_object_record(record) for record in records),
                key=lambda item: (item["sha256"], item["object_id"]),
            )
            files.append(
                BundleInput(
                    path="archive/referenced-objects.json",
                    data=canonical_json(public_records),
                )
            )

        stamp = generated_at or datetime.now(UTC)
        if stamp.tzinfo is None:
            raise ExportIntegrityError()
        high_water = _high_water(summaries, upper)
        fd, temporary_name = tempfile.mkstemp(prefix="qingyu-export-", suffix=".zip")
        os.close(fd)
        destination = Path(temporary_name)
        os.chmod(destination, 0o600)
        try:
            manifest_sha256 = build_bundle(
                destination,
                scope=scope,
                conversation_id=str(conversation_id) if conversation_id else None,
                generated_at=stamp.isoformat(),
                high_water=high_water,
                files=files,
            )
        except ExportBundleError as exc:
            destination.unlink(missing_ok=True)
            raise ExportIntegrityError() from exc
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        return ExportBundle(
            path=destination,
            manifest_sha256=manifest_sha256,
            scope=scope,
            conversation_id=str(conversation_id) if conversation_id else None,
        )
