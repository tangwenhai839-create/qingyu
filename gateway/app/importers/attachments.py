from __future__ import annotations

import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ..archive.service import ArchiveIntegrityError, ArchiveService, PreparedObject
from .generic import ParsedAttachment, ParsedConversation


@dataclass(frozen=True)
class AttachmentWork:
    parsed: ParsedAttachment
    availability: str
    prepared: PreparedObject | None = None
    error_code: str | None = None
    observed_filename: str | None = None
    observed_media_type: str | None = None
    observed_size_bytes: int | None = None
    resolution_status: str | None = None
    resolved_member_path: str | None = None


def prepare_attachment_work(
    conversations: tuple[ParsedConversation, ...],
    extract_root: Path,
    archive_service: ArchiveService,
    *,
    source_platform: str,
    source_member: str,
    health_check,
) -> dict[tuple[int, str, str], AttachmentWork]:
    """Classify declarations and publish available bytes without DB writes."""
    root_details = extract_root.lstat()
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if stat.S_ISLNK(root_details.st_mode) or bool(
        getattr(root_details, "st_file_attributes", 0) & reparse_flag
    ):
        raise ValueError("unsafe_attachment_root")
    root = extract_root.resolve(strict=True)
    regular_members: dict[str, Path] = {}
    asset_members: dict[str, list[tuple[str, Path]]] = {}
    unsafe_members: set[str] = set()
    for candidate in extract_root.rglob("*"):
        try:
            candidate.relative_to(root)
        except ValueError:
            # A lexical entry below the extraction root must not resolve outside it.
            # Record it as unsafe and never make its bytes available to the resolver.
            relative = candidate.relative_to(extract_root).as_posix()
            unsafe_members.add(relative)
            continue
        relative = candidate.relative_to(extract_root).as_posix()
        details = candidate.lstat()
        is_reparse = stat.S_ISLNK(details.st_mode) or bool(
            getattr(details, "st_file_attributes", 0) & reparse_flag
        )
        if is_reparse:
            unsafe_members.add(relative)
        elif candidate.is_file():
            regular_members[relative] = candidate
            # The real-export contract is exact and case-sensitive: opaque asset ID
            # ``abc`` can resolve only to a regular member whose stem is exactly
            # ``abc`` and whose final suffix is exactly ``.dat``.  Directories and
            # display filenames are never used as fuzzy fallbacks.
            if candidate.suffix == ".dat":
                asset_members.setdefault(candidate.stem, []).append((relative, candidate))
    result = {}
    for conversation_index, conversation in enumerate(conversations):
        for message in conversation.messages:
            for attachment in message.attachments:
                health_check()
                key = (conversation_index, message.node_id, attachment.stable_attachment_key)
                member_path = attachment.member_path
                if member_path is None and attachment.asset_id is not None:
                    if not _valid_asset_id(attachment.asset_id):
                        result[key] = AttachmentWork(
                            attachment,
                            "referenced_only",
                            resolution_status="unsupported",
                        )
                        continue
                    candidates = asset_members.get(attachment.asset_id, [])
                    if len(candidates) > 1:
                        result[key] = AttachmentWork(
                            attachment,
                            "referenced_only",
                            resolution_status="ambiguous",
                        )
                        continue
                    if not candidates:
                        result[key] = AttachmentWork(
                            attachment,
                            "missing",
                            resolution_status="missing",
                        )
                        continue
                    member_path, _resolved = candidates[0]
                if member_path is None:
                    status = "unsupported" if attachment.source_references else "unresolved"
                    result[key] = AttachmentWork(attachment, "referenced_only", resolution_status=status)
                    continue
                if not _valid_member_path(member_path):
                    result[key] = AttachmentWork(
                        attachment,
                        "referenced_only",
                        error_code="unsafe_attachment_member",
                        resolution_status="unsupported",
                    )
                    continue
                member_parts = Path(member_path).parts
                member_ancestors = {
                    Path(*member_parts[:index]).as_posix()
                    for index in range(1, len(member_parts) + 1)
                }
                if member_ancestors & unsafe_members:
                    result[key] = AttachmentWork(
                        attachment,
                        "failed",
                        error_code="unsafe_attachment_member",
                        resolution_status="unsupported",
                    )
                    continue
                source = regular_members.get(member_path)
                if source is None:
                    result[key] = AttachmentWork(attachment, "missing", resolution_status="missing")
                    continue
                observed_size = source.stat().st_size
                sniffed_media_type = _sniff_media_type(source)
                declared_media_type = attachment.media_type.strip().lower() if attachment.media_type else None
                text_media_type = (
                    declared_media_type
                    if declared_media_type is not None
                    and declared_media_type.startswith("text/")
                    and _is_utf8_text(source)
                    else None
                )
                observed_media_type = (
                    sniffed_media_type
                    or text_media_type
                    or "application/octet-stream"
                )
                observed = {
                    "observed_filename": source.name,
                    "observed_media_type": observed_media_type,
                    "observed_size_bytes": observed_size,
                }
                if attachment.size_bytes is not None and attachment.size_bytes != observed_size:
                    result[key] = AttachmentWork(
                        attachment,
                        "failed",
                        error_code="attachment_size_mismatch",
                        resolution_status="resolved",
                        resolved_member_path=member_path,
                        **observed,
                    )
                    continue
                if (
                    attachment.media_type is not None
                    and attachment.media_type.strip().lower() != observed_media_type.lower()
                ):
                    result[key] = AttachmentWork(
                        attachment,
                        "failed",
                        error_code="attachment_media_type_mismatch",
                        resolution_status="resolved",
                        resolved_member_path=member_path,
                        **observed,
                    )
                    continue
                try:
                    prepared = archive_service.prepare(
                        source,
                        source.name,
                        source_platform,
                        observed_media_type,
                        logical_path=member_path,
                        observation_metadata={
                            "source_member": source_member,
                            "attachment_member_path": member_path,
                            "raw_json_pointer": attachment.raw_json_pointer,
                            "declared_filename": attachment.filename,
                            "declared_media_type": attachment.media_type,
                            "declared_size_bytes": attachment.size_bytes,
                        },
                    )
                except (ArchiveIntegrityError, OSError, ValueError) as exc:
                    result[key] = AttachmentWork(
                        attachment,
                        "failed",
                        error_code=type(exc).__name__,
                        resolution_status="resolved",
                        resolved_member_path=member_path,
                        **observed,
                    )
                else:
                    result[key] = AttachmentWork(
                        attachment,
                        "available",
                        prepared=prepared,
                        resolution_status="resolved",
                        resolved_member_path=member_path,
                        **observed,
                    )
    return result


def _valid_asset_id(asset_id: str) -> bool:
    """Validate a source opaque ID without interpreting it as a path."""
    return bool(asset_id) and asset_id not in {".", ".."} and not any(
        separator in asset_id for separator in ("/", "\\", "\x00")
    )


def _valid_member_path(member_path: str) -> bool:
    """Accept only the canonical POSIX member form produced by safe_extract."""
    if (
        not member_path
        or member_path.startswith(("/", "\\"))
        or re.match(r"^[A-Za-z]:", member_path)
        or "\\" in member_path
        or "\x00" in member_path
    ):
        return False
    path = PurePosixPath(member_path)
    return (
        not path.is_absolute()
        and path.as_posix() == member_path
        and all(part not in {"", ".", ".."} for part in path.parts)
    )


def _sniff_media_type(path: Path) -> str | None:
    with path.open("rb") as source:
        head = source.read(32)
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    if head.startswith((b"RIFF",)) and head[8:12] == b"WAVE":
        return "audio/wav"
    if head.startswith(b"ID3") or head[:2] in {b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"}:
        return "audio/mpeg"
    if head.startswith(b"OggS"):
        return "audio/ogg"
    if head.startswith(b"\x1aE\xdf\xa3"):
        return "audio/webm"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def _is_utf8_text(path: Path) -> bool:
    try:
        payload = path.read_bytes()
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return "\x00" not in text
