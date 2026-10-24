import copy
from dataclasses import dataclass, replace
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..archive.service import (
    ArchiveIntegrityError,
    ArchiveService,
    PendingArchiveResult,
)
from ..models import (
    ArchiveObject,
    ArchiveObservation,
    Attachment,
    AttachmentObservation,
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
from .attachments import AttachmentWork
from .generic import GenericTreeParser
from .real_chatgpt import (
    FRAGMENT_FINGERPRINT_VERSION,
    OBSERVATION_FINGERPRINT_VERSION,
    SEMANTIC_FINGERPRINT_VERSION,
    normalize_timestamp,
)
from .report import empty_report
from .revision import (
    compute_canonical_fragment_fingerprint,
    compute_revision_fingerprint,
    compute_semantic_revision_fingerprint,
    compute_source_observation_fingerprint,
    compute_text_hash,
)
from .tree import TreeValidator
from .worker import ImportWorker, LeaseLost, LeaseRequired


@dataclass(frozen=True)
class ImportOutcome:
    report: dict
    counts: dict


class BatchReplayIdentityError(RuntimeError):
    pass


class AttachmentPersistenceError(RuntimeError):
    def __init__(self, key, parsed):
        super().__init__("attachment_persistence_failed")
        self.key = key
        self.parsed = parsed

class ImportService:
    """Persist fixture conversations in short, ownership-fenced transactions."""
    def __init__(self, db: Session): self.db = db

    def import_fixture(self, payload: dict[str, Any], archive_object_id, platform="fixture", account_label="default", job: ImportJob | None = None, lease_token=None, source_member="fixture.json", parser=None, unknown_entries=None, health_check=None, parsed_conversations=None, attachment_work=None, attachment_archive: ArchiveService | None = None) -> ImportOutcome:
        health_check = health_check or (lambda: None)
        if job is None or lease_token is None:
            raise LeaseRequired("lease_required")
        account = self.db.get(SourceAccount, job.source_account_id)
        if account is None:
            raise ValueError("invalid_source_account")
        archive = self.db.get(ArchiveObject, job.archive_object_id)
        if archive is None:
            raise ValueError("archive_object_missing")
        archive_object_id = archive.id
        account_id = account.id
        archive_sha256 = archive.sha256
        parser = parser or GenericTreeParser()
        report = empty_report(parser.name, parser.version, "matched")
        initial_unknown = copy.deepcopy(unknown_entries or [])
        parsed_conversations = (
            tuple(parsed_conversations)
            if parsed_conversations is not None
            else parser.parse(payload)
        )
        attachment_work = attachment_work or {}
        attempt_number = int(job.attempt_count)
        try:
            for conv_index, parsed in enumerate(parsed_conversations):
                health_check()
                owned_job = ImportWorker(self.db).assert_ownership(job, lease_token)
                attempt_number = int(owned_job.attempt_count)
                real_export = parser.name == "chatgpt-export" and bool(parsed.nodes)
                if any(message.attachments for message in parsed.messages) and attempt_number <= 0:
                    raise ValueError("invalid_attempt_number")
                self.db.scalar(select(SourceAccount.id).where(SourceAccount.id == account_id).with_for_update())
                external_id = parsed.external_id
                synthetic = not bool(external_id)
                if synthetic:
                    external_id = f"synthetic:{archive_sha256}:{source_member}:conversation:{conv_index}"
                sequence_no = conv_index + 1
                batch_key = f"{source_member}:conversation:{conv_index}"
                existing_batch = self.db.scalar(
                    select(ImportBatchCommit).where(
                        ImportBatchCommit.import_job_id == job.id,
                        or_(ImportBatchCommit.sequence_no == sequence_no, ImportBatchCommit.batch_key == batch_key),
                    )
                )
                if existing_batch is not None:
                    _validate_replay(existing_batch, sequence_no, batch_key, external_id, parser.name, parser.version)
                    _merge_report(report, existing_batch.report_delta, include_attachments=False)
                elif real_export or owned_job.parser_name == "pending-detection":
                    # Parser detection becomes durable with the first evidence batch.
                    # Before that point the Job deliberately remains pending-detection.
                    owned_job.parser_name = parser.name
                    owned_job.parser_version = parser.version

                before = copy.deepcopy(report)
                if existing_batch is None:
                    # Parser-level warnings become part of the first newly
                    # durable batch so crash/retry reconstruction cannot lose
                    # warnings first observed on a later attempt.
                    for item in initial_unknown:
                        if item not in report["unknown"]:
                            report["unknown"].append(copy.deepcopy(item))
                if synthetic and existing_batch is None:
                    report["unknown"].append({"type": "missing_external_conversation_id", "severity": "warning", "conversation_identity": external_id, "detail": "generated deterministic synthetic identity", "archive_object_id": str(archive_object_id), "zip_member": source_member, "raw_pointer": f"/conversations/{conv_index}"})
                conversation = self.db.scalar(select(Conversation).where(Conversation.source_account_id == account_id, Conversation.external_conversation_id == external_id))
                if conversation is None:
                    if existing_batch is not None:
                        raise BatchReplayIdentityError("batch_replay_conversation_missing")
                    conversation = Conversation(source_account_id=account_id, external_conversation_id=external_id, title=parsed.title, metadata_json={"identity_kind": "synthetic" if synthetic else "external"}); self.db.add(conversation); self.db.flush(); report["conversations"]["new"] += 1
                elif existing_batch is not None:
                    pass
                elif conversation.title != parsed.title:
                    conversation.title = parsed.title; report["conversations"]["changed"] += 1
                else: report["conversations"]["existing"] += 1
                resolved_nodes = {}
                if real_export:
                    for node in parsed.nodes:
                        entity = self.db.scalar(select(ConversationNode).where(
                            ConversationNode.conversation_id == conversation.id,
                            ConversationNode.external_node_id == node.node_id,
                        ))
                        if entity is None:
                            if existing_batch is not None:
                                raise BatchReplayIdentityError("batch_replay_node_missing")
                            entity = ConversationNode(conversation_id=conversation.id, external_node_id=node.node_id)
                            self.db.add(entity)
                            self.db.flush()
                        resolved_nodes[node.node_id] = entity

                resolved, revisions = {}, {}
                for item in parsed.messages:
                    message_identity = item.external_message_id if real_export else item.node_id
                    message = self.db.scalar(select(Message).where(Message.conversation_id == conversation.id, Message.external_message_id == message_identity))
                    if message is None:
                        if existing_batch is not None:
                            raise BatchReplayIdentityError("batch_replay_message_missing")
                        message = Message(conversation_id=conversation.id, external_message_id=message_identity); self.db.add(message); self.db.flush(); report["messages"]["new"] += 1
                    elif existing_batch is None: report["messages"]["existing"] += 1
                    digest = compute_text_hash(item.content_text)
                    semantic_fingerprint = compute_semantic_revision_fingerprint(item.semantic_evidence_json, SEMANTIC_FINGERPRINT_VERSION) if real_export else None
                    fingerprint = semantic_fingerprint or compute_revision_fingerprint(role=item.role, content_text=item.content_text, content_json=item.content_json, model=item.model, raw_timestamp=item.raw_timestamp, attachments=item.attachments)
                    revision_filter = MessageRevision.semantic_revision_fingerprint == semantic_fingerprint if real_export else MessageRevision.revision_fingerprint == fingerprint
                    revision = self.db.scalar(select(MessageRevision).where(MessageRevision.message_id == message.id, revision_filter))
                    if revision is None:
                        if existing_batch is not None:
                            raise BatchReplayIdentityError("batch_replay_revision_missing")
                        latest = self.db.scalar(select(MessageRevision).where(MessageRevision.message_id == message.id).order_by(MessageRevision.revision_no.desc()))
                        pointer = item.raw_json_pointer or f"/conversations/{conv_index}/mapping/{json_pointer_escape(item.node_id)}"
                        raw_create = item.semantic_evidence_json.get("create_time") if real_export else None
                        create_normalized, create_status = normalize_timestamp(raw_create) if real_export else (None, None)
                        update_normalized, update_status = normalize_timestamp(item.raw_update_timestamp) if real_export else (None, None)
                        revision = MessageRevision(
                            message_id=message.id, revision_no=(latest.revision_no + 1 if latest else 1),
                            role=item.role, content_text=item.content_text, content_json=item.content_json,
                            model=item.model, raw_timestamp=item.raw_timestamp,
                            normalized_timestamp=create_normalized, raw_create_time_json=raw_create,
                            raw_update_time_json=item.raw_update_timestamp if real_export else None,
                            normalized_update_timestamp=update_normalized,
                            create_time_parse_status=create_status, update_time_parse_status=update_status,
                            time_provenance_json={"source": "chatgpt-export", "precision": "source-declared"} if real_export else None,
                            content_type=item.content_type, semantic_evidence_json=item.semantic_evidence_json,
                            semantic_revision_fingerprint=semantic_fingerprint,
                            fingerprint_version=SEMANTIC_FINGERPRINT_VERSION if real_export else None,
                            content_hash=digest, revision_fingerprint=fingerprint,
                            archive_object_id=archive_object_id, raw_json_pointer=pointer,
                        ); self.db.add(revision); self.db.flush(); report["revisions"]["new"] += 1
                    elif existing_batch is None: report["revisions"]["existing"] += 1
                    resolved[item.node_id], revisions[item.node_id] = message, revision
                if existing_batch is None:
                    if not real_export:
                        report["tree_anomalies"].extend(TreeValidator().validate(parsed.messages))
                    for order, item in enumerate(parsed.messages):
                        if self.db.scalar(select(MessageObservation).where(MessageObservation.import_job_id == job.id, MessageObservation.external_node_id == item.node_id, MessageObservation.message_revision_id == revisions[item.node_id].id)): continue
                        parent = resolved.get(item.parent_node_id)
                        pointer = item.raw_json_pointer or f"/conversations/{conv_index}/mapping/{json_pointer_escape(item.node_id)}"
                        observation_fingerprint = compute_source_observation_fingerprint(item.observation_evidence_json, OBSERVATION_FINGERPRINT_VERSION) if real_export else None
                        mapping_pointer = pointer[:-len("/message")] if real_export and pointer.endswith("/message") else pointer
                        self.db.add(MessageObservation(import_job_id=job.id, message_id=resolved[item.node_id].id, message_revision_id=revisions[item.node_id].id, external_node_id=item.node_id, external_parent_node_id=item.parent_node_id, parent_message_id=parent.id if parent else None, branch_key=item.branch_key, is_current_path=item.node_id == parsed.current_node_id, observation_order=order, raw_mapping_pointer=mapping_pointer, raw_message_pointer=pointer if real_export else None, metadata_json={"archive_object_id": str(archive.id), "zip_member": source_member}, source_observation_json=item.observation_evidence_json if real_export else None, source_observation_fingerprint=observation_fingerprint, canonical_fragment_fingerprint=compute_canonical_fragment_fingerprint(item.source_fragment_json, pointer) if real_export else None, fragment_fingerprint_version=FRAGMENT_FINGERPRINT_VERSION if real_export else None))

                pending_archives = []
                declared_work = []
                for item in parsed.messages:
                    for declared in item.attachments:
                        key = (conv_index, item.node_id, declared.stable_attachment_key)
                        work = attachment_work.get(key)
                        if work is None:
                            work = AttachmentWork(declared, "referenced_only")
                        declared_work.append((key, item, work))
                declared_work.sort(key=lambda entry: _attachment_lock_order(entry[2]))
                for key, item, work in declared_work:
                    try:
                        pending = self._persist_attachment(
                            job=job,
                            attempt_number=attempt_number,
                            revision=revisions[item.node_id],
                            work=work,
                            source_member=source_member,
                            archive_service=attachment_archive,
                            durable_replay=real_export and existing_batch is not None,
                        )
                    except (ArchiveIntegrityError, OSError) as exc:
                        raise AttachmentPersistenceError(key, work.parsed) from exc
                    if pending is not None:
                        pending_archives.append(pending)
                    report["attachments"][work.availability] += 1
                health_check()
                ImportWorker(self.db).assert_ownership(job, lease_token)
                if existing_batch is None:
                    delta = _report_delta(before, report)
                    delta_counts = _counts_from_report(delta)
                    batch = ImportBatchCommit(
                        import_job_id=job.id,
                        conversation_id=conversation.id,
                        sequence_no=sequence_no,
                        batch_key=batch_key,
                        external_conversation_id=external_id,
                        parser_name=parser.name,
                        contract_version=parser.version,
                        report_delta=delta,
                        counts_delta=delta_counts,
                    )
                    self.db.add(batch)
                    self.db.flush()
                    if real_export:
                        self._persist_real_export_snapshot(
                            batch=batch,
                            conversation=conversation,
                            parsed=parsed,
                            conversation_index=conv_index,
                            archive_object_id=archive_object_id,
                            source_member=source_member,
                            resolved_nodes=resolved_nodes,
                            resolved_messages=resolved,
                            revisions=revisions,
                        )
                elif real_export:
                    self._validate_real_export_snapshot(
                        batch=existing_batch,
                        parsed=parsed,
                        archive_object_id=archive_object_id,
                        source_member=source_member,
                        resolved_nodes=resolved_nodes,
                        resolved_messages=resolved,
                        revisions=revisions,
                    )
                self.db.commit()
                for pending in pending_archives:
                    attachment_archive.finalize(pending)
                batch_has_failed_attachment = any(
                    work.availability == "failed" for _key, _item, work in declared_work
                )
                if not batch_has_failed_attachment and not ImportWorker(self.db).advance_checkpoint(
                    job, lease_token, sequence_no, batch_key
                ):
                    raise LeaseLost("ownership_lost")
                if batch_has_failed_attachment:
                    break
            # All-existing replays create no new ledger row. Preserve warnings
            # rediscovered by the current parser for terminal overlay.
            for item in initial_unknown:
                if item not in report["unknown"]:
                    report["unknown"].append(copy.deepcopy(item))
            report["attachments"] = self._attachment_counts(job.id, attempt_number)
            report["items"] = {"conversations": sum(report["conversations"].values()), "messages": sum(report["messages"].values())}
            counts = {"items_discovered": report["items"]["messages"], "items_imported": report["messages"]["new"], "discovered_count": report["items"]["messages"], "imported_count": report["messages"]["new"], "skipped_count": report["messages"]["existing"], "failed_count": report["messages"]["failed"]}
        except AttachmentPersistenceError as exc:
            self.db.rollback()
            retry_work = dict(attachment_work)
            retry_work[exc.key] = AttachmentWork(
                exc.parsed,
                "failed",
                error_code="attachment_archive_failed",
            )
            return self.import_fixture(
                payload,
                archive_object_id,
                platform=platform,
                account_label=account_label,
                job=job,
                lease_token=lease_token,
                source_member=source_member,
                parser=parser,
                unknown_entries=unknown_entries,
                health_check=health_check,
                parsed_conversations=parsed_conversations,
                attachment_work=retry_work,
                attachment_archive=attachment_archive,
            )
        except Exception:
            self.db.rollback()
            raise
        return ImportOutcome(report=report, counts=counts)

    def _persist_real_export_snapshot(
        self, *, batch, conversation, parsed, conversation_index, archive_object_id,
        source_member, resolved_nodes, resolved_messages, revisions,
    ):
        conversation_pointer = parsed.raw_json_pointer or f"/{conversation_index}"
        conversation_envelope = copy.deepcopy(parsed.source_conversation_json or {})
        create_normalized, create_status = normalize_timestamp(parsed.raw_create_timestamp)
        update_normalized, update_status = normalize_timestamp(parsed.raw_update_timestamp)
        current_present = "current_node" in conversation_envelope
        current_external = parsed.current_node_id
        current_entity = resolved_nodes.get(current_external) if current_external is not None else None
        current_status = (
            "absent" if not current_present else
            "null" if current_external is None else
            "resolved" if current_entity is not None else "missing"
        )
        conversation_observation = ConversationObservation(
            import_batch_commit_id=batch.id,
            conversation_id=conversation.id,
            archive_object_id=archive_object_id,
            source_member=source_member,
            source_conversation_index=conversation_index,
            raw_conversation_pointer=conversation_pointer,
            external_current_node_id=current_external,
            current_conversation_node_id=current_entity.id if current_entity else None,
            current_node_status=current_status,
            source_conversation_json=conversation_envelope,
            raw_create_time_json=parsed.raw_create_timestamp,
            raw_update_time_json=parsed.raw_update_timestamp,
            normalized_created_at=create_normalized,
            normalized_updated_at=update_normalized,
            create_time_parse_status=create_status,
            update_time_parse_status=update_status,
            time_provenance_json={"source": "chatgpt-export", "precision": "source-declared"},
            source_observation_fingerprint=compute_source_observation_fingerprint(conversation_envelope),
            canonical_fragment_fingerprint=compute_canonical_fragment_fingerprint(parsed.source_fragment_json, conversation_pointer),
            fragment_fingerprint_version=FRAGMENT_FINGERPRINT_VERSION,
        )
        self.db.add(conversation_observation)
        self.db.flush()
        for order, node in enumerate(parsed.nodes):
            node_entity = resolved_nodes[node.node_id]
            parent = resolved_nodes.get(node.parent_node_id)
            message = resolved_messages.get(node.node_id)
            revision = revisions.get(node.node_id)
            node_evidence = {
                "node": node.source_node_json,
                "message_presence": node.message_presence,
                "semantic_revision_fingerprint": revision.semantic_revision_fingerprint if revision else None,
                "source_observation_fingerprint": (
                    compute_source_observation_fingerprint(node.message.observation_evidence_json)
                    if node.message is not None else None
                ),
            }
            self.db.add(MappingNodeObservation(
                conversation_observation_id=conversation_observation.id,
                conversation_id=conversation.id,
                conversation_node_id=node_entity.id,
                parent_conversation_node_id=parent.id if parent else None,
                external_parent_node_id=node.parent_node_id,
                message_id=message.id if message else None,
                message_revision_id=revision.id if revision else None,
                message_presence=node.message_presence,
                observation_order=order,
                raw_mapping_pointer=node.raw_json_pointer,
                declared_children_json=list(node.declared_children) if node.declared_children is not None else None,
                source_node_json=node.source_node_json,
                source_observation_fingerprint=compute_source_observation_fingerprint(node_evidence),
                canonical_fragment_fingerprint=compute_canonical_fragment_fingerprint(node.source_fragment_json, node.raw_json_pointer),
                fragment_fingerprint_version=FRAGMENT_FINGERPRINT_VERSION,
            ))

    def _validate_real_export_snapshot(
        self, *, batch, parsed, archive_object_id, source_member,
        resolved_nodes, resolved_messages, revisions,
    ):
        observation = self.db.scalar(select(ConversationObservation).where(
            ConversationObservation.import_batch_commit_id == batch.id
        ))
        if observation is None:
            raise BatchReplayIdentityError("batch_replay_conversation_observation_missing")
        envelope = copy.deepcopy(parsed.source_conversation_json or {})
        conversation_pointer = parsed.raw_json_pointer or f"/{batch.sequence_no - 1}"
        create_normalized, create_status = normalize_timestamp(parsed.raw_create_timestamp)
        update_normalized, update_status = normalize_timestamp(parsed.raw_update_timestamp)
        current_present = "current_node" in envelope
        current_entity = resolved_nodes.get(parsed.current_node_id) if parsed.current_node_id is not None else None
        current_status = (
            "absent" if not current_present else
            "null" if parsed.current_node_id is None else
            "resolved" if current_entity is not None else "missing"
        )
        expected_conversation_fingerprint = compute_source_observation_fingerprint(envelope)
        actual_conversation = (
            observation.conversation_id,
            observation.archive_object_id,
            observation.source_member,
            observation.source_conversation_index,
            observation.raw_conversation_pointer,
            observation.external_current_node_id,
            observation.current_conversation_node_id,
            observation.current_node_status,
            observation.source_conversation_json,
            observation.raw_create_time_json,
            observation.raw_update_time_json,
            observation.normalized_created_at,
            observation.normalized_updated_at,
            observation.create_time_parse_status,
            observation.update_time_parse_status,
            observation.time_provenance_json,
            observation.source_observation_fingerprint,
            observation.canonical_fragment_fingerprint,
            observation.fragment_fingerprint_version,
        )
        expected_conversation = (
            batch.conversation_id,
            archive_object_id,
            source_member,
            batch.sequence_no - 1,
            conversation_pointer,
            parsed.current_node_id,
            current_entity.id if current_entity else None,
            current_status,
            envelope,
            parsed.raw_create_timestamp,
            parsed.raw_update_timestamp,
            create_normalized,
            update_normalized,
            create_status,
            update_status,
            {"source": "chatgpt-export", "precision": "source-declared"},
            expected_conversation_fingerprint,
            compute_canonical_fragment_fingerprint(parsed.source_fragment_json, conversation_pointer),
            FRAGMENT_FINGERPRINT_VERSION,
        )
        if actual_conversation != expected_conversation:
            raise BatchReplayIdentityError("batch_replay_conversation_observation_mismatch")
        for item in parsed.messages:
            message = resolved_messages[item.node_id]
            revision = revisions[item.node_id]
            raw_create = item.semantic_evidence_json.get("create_time")
            create_normalized, create_status = normalize_timestamp(raw_create)
            update_normalized, update_status = normalize_timestamp(item.raw_update_timestamp)
            pointer = item.raw_json_pointer or f"/{batch.sequence_no - 1}/mapping/{json_pointer_escape(item.node_id)}"
            semantic_fingerprint = compute_semantic_revision_fingerprint(
                item.semantic_evidence_json, SEMANTIC_FINGERPRINT_VERSION
            )
            actual_revision = (
                revision.message_id,
                revision.role,
                revision.content_text,
                revision.content_json,
                revision.model,
                revision.raw_timestamp,
                revision.normalized_timestamp,
                revision.raw_create_time_json,
                revision.raw_update_time_json,
                revision.normalized_update_timestamp,
                revision.create_time_parse_status,
                revision.update_time_parse_status,
                revision.time_provenance_json,
                revision.content_type,
                revision.semantic_evidence_json,
                revision.semantic_revision_fingerprint,
                revision.fingerprint_version,
                revision.content_hash,
                revision.revision_fingerprint,
                revision.archive_object_id,
                revision.raw_json_pointer,
            )
            expected_revision = (
                message.id,
                item.role,
                item.content_text,
                item.content_json,
                item.model,
                item.raw_timestamp,
                create_normalized,
                raw_create,
                item.raw_update_timestamp,
                update_normalized,
                create_status,
                update_status,
                {"source": "chatgpt-export", "precision": "source-declared"},
                item.content_type,
                item.semantic_evidence_json,
                semantic_fingerprint,
                SEMANTIC_FINGERPRINT_VERSION,
                compute_text_hash(item.content_text),
                semantic_fingerprint,
                archive_object_id,
                pointer,
            )
            if actual_revision != expected_revision:
                raise BatchReplayIdentityError("batch_replay_revision_evidence_mismatch")
        rows = list(self.db.scalars(
            select(MappingNodeObservation)
            .where(MappingNodeObservation.conversation_observation_id == observation.id)
            .order_by(MappingNodeObservation.observation_order)
        ))
        if len(rows) != len(parsed.nodes):
            raise BatchReplayIdentityError("batch_replay_node_set_mismatch")
        for order, (row, node) in enumerate(zip(rows, parsed.nodes, strict=True)):
            revision = revisions.get(node.node_id)
            node_evidence = {
                "node": node.source_node_json,
                "message_presence": node.message_presence,
                "semantic_revision_fingerprint": revision.semantic_revision_fingerprint if revision else None,
                "source_observation_fingerprint": (
                    compute_source_observation_fingerprint(node.message.observation_evidence_json)
                    if node.message is not None else None
                ),
            }
            expected = (
                resolved_nodes[node.node_id].id,
                resolved_nodes.get(node.parent_node_id).id if resolved_nodes.get(node.parent_node_id) else None,
                node.parent_node_id,
                resolved_messages.get(node.node_id).id if resolved_messages.get(node.node_id) else None,
                revision.id if revision else None,
                node.message_presence,
                order,
                node.raw_json_pointer,
                list(node.declared_children) if node.declared_children is not None else None,
                node.source_node_json,
                compute_source_observation_fingerprint(node_evidence),
                compute_canonical_fragment_fingerprint(node.source_fragment_json, node.raw_json_pointer),
                FRAGMENT_FINGERPRINT_VERSION,
            )
            actual = (
                row.conversation_id, row.conversation_node_id, row.parent_conversation_node_id,
                row.external_parent_node_id, row.message_id, row.message_revision_id,
                row.message_presence, row.observation_order, row.raw_mapping_pointer,
                row.declared_children_json, row.source_node_json,
                row.source_observation_fingerprint, row.canonical_fragment_fingerprint,
                row.fragment_fingerprint_version,
            )
            expected = (batch.conversation_id, *expected)
            if actual != expected:
                raise BatchReplayIdentityError("batch_replay_node_observation_mismatch")

        expected_message_observations = []
        for order, item in enumerate(parsed.messages):
            message = resolved_messages[item.node_id]
            revision = revisions[item.node_id]
            parent = resolved_messages.get(item.parent_node_id)
            pointer = item.raw_json_pointer or f"/{batch.sequence_no - 1}/mapping/{json_pointer_escape(item.node_id)}"
            observation_fingerprint = compute_source_observation_fingerprint(item.observation_evidence_json)
            expected_message_observations.append((
                message.id,
                revision.id,
                item.node_id,
                item.parent_node_id,
                parent.id if parent else None,
                item.branch_key,
                item.node_id == parsed.current_node_id,
                order,
                pointer.removesuffix("/message"),
                pointer,
                {"archive_object_id": str(archive_object_id), "zip_member": source_member},
                item.observation_evidence_json,
                observation_fingerprint,
                compute_canonical_fragment_fingerprint(
                    item.source_fragment_json,
                    pointer,
                ),
                FRAGMENT_FINGERPRINT_VERSION,
            ))
        message_rows = list(self.db.scalars(
            select(MessageObservation)
            .where(
                MessageObservation.import_job_id == batch.import_job_id,
                MessageObservation.message_revision_id.in_(
                    [revision.id for revision in revisions.values()]
                ),
            )
            .order_by(MessageObservation.observation_order)
        )) if revisions else []
        actual_message_observations = [(
            row.message_id,
            row.message_revision_id,
            row.external_node_id,
            row.external_parent_node_id,
            row.parent_message_id,
            row.branch_key,
            row.is_current_path,
            row.observation_order,
            row.raw_mapping_pointer,
            row.raw_message_pointer,
            row.metadata_json,
            row.source_observation_json,
            row.source_observation_fingerprint,
            row.canonical_fragment_fingerprint,
            row.fragment_fingerprint_version,
        ) for row in message_rows]
        if actual_message_observations != expected_message_observations:
            raise BatchReplayIdentityError("batch_replay_message_observation_mismatch")

    def _persist_attachment(
        self, *, job, attempt_number, revision, work, source_member,
        archive_service, durable_replay=False,
    ):
        declared = work.parsed
        attachment = self.db.scalar(
            select(Attachment).where(
                Attachment.message_revision_id == revision.id,
                Attachment.stable_attachment_key == declared.stable_attachment_key,
            )
        )
        stable_values = (
            declared.external_attachment_id,
            declared.original_reference,
            declared.filename,
            declared.media_type,
            declared.size_bytes,
            declared.descriptor_json,
        )
        if attachment is None:
            attachment = Attachment(
                message_revision_id=revision.id,
                stable_attachment_key=declared.stable_attachment_key,
                external_attachment_id=declared.external_attachment_id,
                original_reference=declared.original_reference,
                filename=declared.filename,
                media_type=declared.media_type,
                size_bytes=declared.size_bytes,
                descriptor_json=declared.descriptor_json,
            )
            self.db.add(attachment)
            self.db.flush()
        elif stable_values != (
            attachment.external_attachment_id,
            attachment.original_reference,
            attachment.filename,
            attachment.media_type,
            attachment.size_bytes,
            attachment.descriptor_json,
        ):
            raise BatchReplayIdentityError("attachment_identity_mismatch")

        existing_query = select(AttachmentObservation).where(
            AttachmentObservation.import_job_id == job.id,
            AttachmentObservation.attachment_id == attachment.id,
        )
        if durable_replay:
            existing_query = existing_query.order_by(AttachmentObservation.attempt_number)
        else:
            existing_query = existing_query.where(
                AttachmentObservation.attempt_number == attempt_number
            )
        existing = self.db.scalar(existing_query)
        if durable_replay and existing is None:
            raise BatchReplayIdentityError("attachment_observation_replay_missing")
        if existing is not None:
            expected_references = list(declared.source_references) if declared.source_references else None
            expected_fragment = compute_canonical_fragment_fingerprint(
                declared.source_fragment_json,
                declared.raw_json_pointer,
            ) if declared.source_references else None
            if (
                existing.availability != work.availability
                or existing.resolution_status != work.resolution_status
                or existing.source_member != source_member
                or existing.attachment_member_path != (work.resolved_member_path or declared.member_path)
                or existing.raw_json_pointer != declared.raw_json_pointer
                or existing.observed_filename != work.observed_filename
                or existing.observed_media_type != work.observed_media_type
                or existing.observed_size_bytes != work.observed_size_bytes
                or existing.metadata_json != ({"error_code": work.error_code} if work.error_code else {})
                or existing.source_references_json != expected_references
                or existing.canonical_fragment_fingerprint != expected_fragment
                or existing.fragment_fingerprint_version != (FRAGMENT_FINGERPRINT_VERSION if expected_fragment else None)
            ):
                raise BatchReplayIdentityError("attachment_observation_replay_mismatch")
            if work.availability == "available":
                if work.prepared is None or archive_service is None or existing.archive_object_id is None:
                    raise BatchReplayIdentityError("attachment_observation_replay_mismatch")
                record = self.db.get(ArchiveObject, existing.archive_object_id)
                provenance = self.db.scalar(
                    select(ArchiveObservation).where(
                        ArchiveObservation.import_job_id == job.id,
                        ArchiveObservation.archive_object_id == existing.archive_object_id,
                        ArchiveObservation.metadata_json["stable_attachment_key"].astext
                        == declared.stable_attachment_key,
                        ArchiveObservation.metadata_json["attempt_number"].astext
                        == str(existing.attempt_number),
                        ArchiveObservation.metadata_json["raw_json_pointer"].astext
                        == declared.raw_json_pointer,
                    ).order_by(ArchiveObservation.id)
                )
                if (
                    record is None
                    or provenance is None
                    or record.sha256 != work.prepared.sha256
                    or record.size_bytes != work.prepared.size_bytes
                    or record.relative_path != work.prepared.relative_path
                ):
                    raise BatchReplayIdentityError("attachment_archive_replay_mismatch")
                return PendingArchiveResult(
                    prepared=work.prepared,
                    object_id=record.id,
                    observation_id=provenance.id,
                    record=record,
                    observation=provenance,
                )
            return None

        pending = None
        if work.availability == "available":
            if work.prepared is None:
                raise ValueError("available_attachment_not_prepared")
            if archive_service is None:
                raise ValueError("attachment_archive_required")
            prepared = replace(
                work.prepared,
                observation_metadata={
                    **work.prepared.observation_metadata,
                    "stable_attachment_key": declared.stable_attachment_key,
                    "attempt_number": attempt_number,
                    "raw_json_pointer": declared.raw_json_pointer,
                },
            )
            pending = archive_service.persist_prepared(prepared, import_job_id=job.id)
        metadata = {"error_code": work.error_code} if work.error_code else {}
        self.db.add(AttachmentObservation(
            import_job_id=job.id,
            attempt_number=attempt_number,
            attachment_id=attachment.id,
            availability=work.availability,
            archive_object_id=pending.object_id if pending else None,
            source_member=source_member,
            attachment_member_path=work.resolved_member_path or declared.member_path,
            raw_json_pointer=declared.raw_json_pointer,
            observed_filename=work.observed_filename,
            observed_media_type=work.observed_media_type,
            observed_size_bytes=work.observed_size_bytes,
            metadata_json=metadata,
            resolution_status=work.resolution_status,
            source_references_json=list(declared.source_references) if declared.source_references else None,
            canonical_fragment_fingerprint=compute_canonical_fragment_fingerprint(
                declared.source_fragment_json,
                declared.raw_json_pointer,
            ) if declared.source_references else None,
            fragment_fingerprint_version=FRAGMENT_FINGERPRINT_VERSION if declared.source_references else None,
        ))
        return pending

    def _attachment_counts(self, job_id, attempt_number):
        counts = {key: 0 for key in ("available", "missing", "referenced_only", "failed")}
        observations = self.db.scalars(
            select(AttachmentObservation)
            .where(AttachmentObservation.import_job_id == job_id)
            .order_by(AttachmentObservation.attachment_id, AttachmentObservation.attempt_number)
        )
        chosen_by_attachment = {}
        for observation in observations:
            chosen_by_attachment.setdefault(observation.attachment_id, observation.availability)
            if observation.attempt_number == attempt_number:
                chosen_by_attachment[observation.attachment_id] = observation.availability
        for availability in chosen_by_attachment.values():
            counts[availability] += 1
        return counts


def json_pointer_escape(value: str) -> str:
    return str(value).replace("~", "~0").replace("/", "~1")


def _attachment_lock_order(work: AttachmentWork):
    """Acquire advisory locks in digest order to avoid cross-job deadlocks."""
    if work.prepared is not None:
        return (0, work.prepared.sha256, work.parsed.stable_attachment_key)
    return (1, "", work.parsed.stable_attachment_key)


def _validate_replay(batch, sequence_no, batch_key, external_id, parser_name, contract_version) -> None:
    identity = (
        batch.sequence_no,
        batch.batch_key,
        batch.external_conversation_id,
        batch.parser_name,
        batch.contract_version,
    )
    expected = (sequence_no, batch_key, external_id, parser_name, contract_version)
    if identity != expected:
        raise BatchReplayIdentityError("batch_replay_identity_mismatch")


def _report_delta(before: dict, after: dict) -> dict:
    delta = empty_report(after["parser"]["name"], after["parser"]["version"], after["parser"]["detection"])
    for section in ("conversations", "messages", "revisions", "attachments"):
        for key in delta[section]:
            delta[section][key] = after[section][key] - before[section][key]
    for section in ("tree_anomalies", "unknown", "errors"):
        delta[section] = copy.deepcopy(after[section][len(before[section]):])
    return delta


def _merge_report(target: dict, delta: dict, *, include_attachments: bool = True) -> None:
    for section in ("conversations", "messages", "revisions", "attachments"):
        if section == "attachments" and not include_attachments:
            continue
        for key, value in delta.get(section, {}).items():
            target[section][key] += value
    for section in ("tree_anomalies", "unknown", "errors"):
        target[section].extend(copy.deepcopy(delta.get(section, [])))


def _counts_from_report(report: dict) -> dict:
    messages = report["messages"]
    return {
        "discovered_count": sum(messages.values()),
        "imported_count": messages["new"],
        "skipped_count": messages["existing"] + messages["skipped"],
        "failed_count": messages["failed"],
    }
