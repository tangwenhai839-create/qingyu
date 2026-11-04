# Gateway Frozen Maintenance Amendment #2 — Evidence text search

Status: proposed with Phase 2. This amendment does not reopen Gateway Phase 3
and does not change Amendment #1. The three GET routes and
`evidence-query-v1` stay as they are. `contracts/schemas/evidence-query-v1.json`
is not extended.

## What this adds

`POST /api/v1/evidence/search` is a read-only substring search over message
text that is already committed on a mapping-node observation. A response means
the Gateway recorded that text. It is not a similarity rank, a latest-state
answer, or a statement that the text is true.

The route uses the same bearer capability as the GET evidence routes
(`EVIDENCE_READ_API_TOKEN` or the operator token). It writes nothing. Success
and error responses set `Cache-Control: private, no-store`.

The response contract is `evidence-search-v1`, with
`meaning: gateway_recorded_historical_evidence` and
`consistency: keyword_substring_match_v1`.

## Why the index is a bigram GIN, not pg_trgm alone

`pg_trgm` extracts trigrams and, on the `postgres:16` locale `en_US.utf8`,
those grams follow `LC_CTYPE`. Measured on PostgreSQL 16 with that locale:

- `show_trgm('运维')` is not contained in `show_trgm` of a sentence that
  contains 运维.
- After `CREATE INDEX ... USING gin (content_text gin_trgm_ops)` on 8,000
  Chinese rows, `EXPLAIN` of `content_text ILIKE '%运维%'` is a sequential
  scan. Forcing the trigram index returns essentially every row and leaves
  the real matches to a heap recheck, so the planner is right to ignore it.
- The same rows, with `qingyu_text_bigrams(content_text) @> qingyu_text_bigrams('运维')`
  and a `strpos` recheck, are a bitmap index scan on
  `ix_message_revisions_content_bigrams`.

A contiguous query of any length at least 2 has each of its character pairs
inside the text. The GIN index finds those pairs. `strpos(lower(content_text), lower(q)) > 0`
drops pairs that occur out of order. One native expression covers two-character
Chinese and longer queries. The migration does not create `pg_trgm`, so a
database owner does not need an extension.

`public.qingyu_text_bigrams(text)` is an immutable SQL function: distinct
`substr(lower(t), i, 2)` for `i` from 1 to `length(t) - 1`. It is null for
null input. The GIN index is built `CONCURRENTLY`.

## Request and response

Body fields:

- `q`: required. Trimmed. 2–200 characters after trim. One character, or only
  whitespace, is 400 `query_too_short`. Longer than 200 is 400 `query_too_long`.
  A NUL is 400 `query_invalid`.
- `source_platform`, `source_account_id`, `conversation_id`: optional filters.
- `from`, `to`: optional inclusive bounds on `normalized_timestamp`. A timezone
  is required. `from` after `to` is 400 `query_range_invalid`. A null timestamp
  does not match a bound.
- `limit`: 1–50, default 20.
- `cursor`: opaque HMAC continuation. It binds a hash of `q` and the filters,
  not the query text. Resend the same `q` and filters. A mismatched, tampered,
  or wrong-kind cursor is 400 `evidence_cursor_invalid`.

Ordering is `normalized_timestamp DESC NULLS LAST`, then mapping-node
observation id descending. `score` is the non-overlapping occurrence count.
`match_mode` is `bigram`. `snippet` is at most 200 characters around the first
hit.

Each hit carries the ids needed to re-read it on the Amendment #1 routes:
`conversation_observation_id`, `mapping_node_observation_id`, and
`message_revision_id`, plus `message_observation_id` when that row exists.
The hit does not include archive paths, source member names, JSON pointers, or
the mutable conversation title.

Error bodies do not repeat `q`. A malformed JSON body is 422 `query_invalid`
and also does not repeat the body. 401 `authentication_required` matches the
GET routes.

## Privacy

Message text in `snippet` is private evidence, same as `content_text` on the
node route. The query is private too. Application access logs omit the body.
Operators should not log proxy bodies, and should set PostgreSQL
`log_parameter_max_length=0` when statement logging is on.

## Freeze checklist

- Reuses Archive → ImportJob → ImportBatchCommit → mapping-node observation.
  No new evidence table, source table, outbox, task queue, or hash chain.
- No `FACT_*`, derived memory, or Core change.
- `evidence-query-v1` routes and schema are unchanged.
- No MCP, `ingest_requests`, or `QingyuNativeParser`.
- One new Alembic revision, `0017_evidence_search_bigram`. Historical revisions
  are untouched. Downgrade drops the index and the function only.
