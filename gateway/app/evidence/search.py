"""Keyword substring search over committed message revisions.

The predicate is the bigram GIN index from migration 0017, plus a
``strpos`` recheck. Two-character Chinese is the case pg_trgm does not
serve: on en_US.utf8 a trigram index is not selective for those queries
and the planner sequential-scans. Longer queries use the same predicate.
Every bigram of a contiguous substring is contained in the text, and
``strpos`` rejects pairs that are present but not contiguous.

Results are occurrences inside mapping-node observations, which are the
committed evidence chain. A revision that was never observed is not a hit.
This is not a latest-state resolver and it does not rank by similarity.
"""
import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

BIGRAM_INDEX = "ix_message_revisions_content_bigrams"
MIN_QUERY_CHARS = 2
MAX_QUERY_CHARS = 200
SNIPPET_CHARS = 200

SEARCH_SQL = """
SELECT
    co.id AS conversation_observation_id,
    mno.id AS mapping_node_observation_id,
    mr.id AS message_revision_id,
    mo.id AS message_observation_id,
    co.conversation_id AS conversation_id,
    sa.id AS source_account_id,
    sa.platform AS source_platform,
    mr.role AS role,
    mr.normalized_timestamp AS normalized_timestamp,
    mr.content_text AS content_text,
    (
        length(lower(mr.content_text))
        - length(replace(lower(mr.content_text), lower(:q), ''))
    ) / length(:q) AS score
FROM mapping_node_observations AS mno
JOIN message_revisions AS mr ON mr.id = mno.message_revision_id
JOIN conversation_nodes AS cn ON cn.id = mno.conversation_node_id
JOIN conversation_observations AS co ON co.id = mno.conversation_observation_id
JOIN import_batch_commits AS ibc ON ibc.id = co.import_batch_commit_id
JOIN import_jobs AS ij ON ij.id = ibc.import_job_id
JOIN source_accounts AS sa ON sa.id = ij.source_account_id
LEFT JOIN message_observations AS mo
    ON mo.import_job_id = ibc.import_job_id
    AND mo.message_revision_id = mr.id
    AND mo.external_node_id = cn.external_node_id
WHERE mr.content_text IS NOT NULL
  AND public.qingyu_text_bigrams(mr.content_text) @> CAST(:query_bigrams AS text[])
  AND strpos(lower(mr.content_text), lower(:q)) > 0
  AND (:source_platform IS NULL OR sa.platform = :source_platform)
  AND (:source_account_id IS NULL OR sa.id = :source_account_id)
  AND (:conversation_id IS NULL OR co.conversation_id = :conversation_id)
  AND (:time_from IS NULL OR mr.normalized_timestamp >= :time_from)
  AND (:time_to IS NULL OR mr.normalized_timestamp <= :time_to)
  AND (
        :after_id IS NULL
        OR (
            :after_ts IS NULL
            AND mr.normalized_timestamp IS NULL
            AND mno.id < :after_id
        )
        OR (
            :after_ts IS NOT NULL
            AND (
                mr.normalized_timestamp < :after_ts
                OR (mr.normalized_timestamp = :after_ts AND mno.id < :after_id)
                OR mr.normalized_timestamp IS NULL
            )
        )
  )
ORDER BY mr.normalized_timestamp DESC NULLS LAST, mno.id DESC
LIMIT :row_limit
"""


class EvidenceSearchRejected(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class SearchFilters:
    source_platform: str | None = None
    source_account_id: uuid.UUID | None = None
    conversation_id: uuid.UUID | None = None
    time_from: datetime | None = None
    time_to: datetime | None = None

    def fingerprint_payload(self) -> dict[str, str | None]:
        return {
            "source_platform": self.source_platform,
            "source_account_id": str(self.source_account_id) if self.source_account_id else None,
            "conversation_id": str(self.conversation_id) if self.conversation_id else None,
            "from": self.time_from.isoformat() if self.time_from else None,
            "to": self.time_to.isoformat() if self.time_to else None,
        }


@dataclass(frozen=True)
class SearchHit:
    conversation_observation_id: uuid.UUID
    mapping_node_observation_id: uuid.UUID
    message_revision_id: uuid.UUID
    message_observation_id: uuid.UUID | None
    conversation_id: uuid.UUID
    source_account_id: uuid.UUID
    source_platform: str
    role: str
    normalized_timestamp: datetime | None
    snippet: str
    score: int
    match_mode: str


@dataclass(frozen=True)
class SearchPage:
    items: list[SearchHit]
    has_more: bool


def normalize_search_query(q: str) -> str:
    """Return the query string that is matched and bound into the cursor."""
    if "\x00" in q:
        raise EvidenceSearchRejected("query_invalid")
    stripped = q.strip()
    if len(stripped) < MIN_QUERY_CHARS:
        raise EvidenceSearchRejected("query_too_short")
    if len(stripped) > MAX_QUERY_CHARS:
        raise EvidenceSearchRejected("query_too_long")
    return stripped


def search_query_fingerprint(q: str, filters: SearchFilters) -> str:
    document = {"q": q, "filters": filters.fingerprint_payload()}
    raw = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def occurrence_snippet(content: str, query: str, window: int = SNIPPET_CHARS) -> str:
    """A bounded slice of ``content`` around the first case-folded hit."""
    start_at = content.lower().find(query.lower())
    if start_at < 0:
        piece = content[:window]
        return piece + ("…" if len(content) > window else "")
    if len(query) >= window:
        end = min(len(content), start_at + window)
        prefix = "…" if start_at else ""
        suffix = "…" if end < len(content) else ""
        return prefix + content[start_at:end] + suffix
    pad = window - len(query)
    start = max(0, start_at - pad // 2)
    end = min(len(content), start + window)
    start = max(0, end - window)
    prefix = "…" if start else ""
    suffix = "…" if end < len(content) else ""
    return prefix + content[start:end] + suffix


def _statement():
    return text(SEARCH_SQL).bindparams(
        bindparam("q", type_=String()),
        bindparam("query_bigrams", type_=ARRAY(String())),
        bindparam("source_platform", type_=String()),
        bindparam("source_account_id", type_=PG_UUID(as_uuid=True)),
        bindparam("conversation_id", type_=PG_UUID(as_uuid=True)),
        bindparam("time_from", type_=DateTime(timezone=True)),
        bindparam("time_to", type_=DateTime(timezone=True)),
        bindparam("after_id", type_=PG_UUID(as_uuid=True)),
        bindparam("after_ts", type_=DateTime(timezone=True)),
        bindparam("row_limit", type_=Integer()),
    )


class EvidenceSearchService:
    def __init__(self, db):
        self.db = db

    def search(
        self,
        *,
        q: str,
        limit: int,
        filters: SearchFilters,
        after: tuple[datetime | None, uuid.UUID] | None = None,
    ) -> SearchPage:
        query_bigrams = self.db.execute(
            text("SELECT public.qingyu_text_bigrams(:q)"),
            {"q": q},
        ).scalar_one()
        if not query_bigrams:
            return SearchPage(items=[], has_more=False)
        after_ts = None
        after_id = None
        if after is not None:
            after_ts, after_id = after
        rows = self.db.execute(
            _statement(),
            {
                "q": q,
                "query_bigrams": list(query_bigrams),
                "source_platform": filters.source_platform,
                "source_account_id": filters.source_account_id,
                "conversation_id": filters.conversation_id,
                "time_from": filters.time_from,
                "time_to": filters.time_to,
                "after_ts": after_ts,
                "after_id": after_id,
                "row_limit": limit + 1,
            },
        ).mappings().all()
        has_more = len(rows) > limit
        hits = []
        for row in rows[:limit]:
            content = row["content_text"] or ""
            hits.append(
                SearchHit(
                    conversation_observation_id=row["conversation_observation_id"],
                    mapping_node_observation_id=row["mapping_node_observation_id"],
                    message_revision_id=row["message_revision_id"],
                    message_observation_id=row["message_observation_id"],
                    conversation_id=row["conversation_id"],
                    source_account_id=row["source_account_id"],
                    source_platform=row["source_platform"],
                    role=row["role"],
                    normalized_timestamp=row["normalized_timestamp"],
                    snippet=occurrence_snippet(content, q),
                    score=int(row["score"]),
                    match_mode="bigram",
                )
            )
        return SearchPage(items=hits, has_more=has_more)
