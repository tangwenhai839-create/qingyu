# Qingyu Memory Gateway Boundary Freeze

Status: Phase 3 frozen by Commit 5. Changes require the review defined below.

## Gateway owns

The Gateway owns durable source evidence and the mechanisms required to preserve, validate, replay, and query it:

- raw archive ingestion; immutable `ArchiveObject` and `ArchiveObservation`; archive integrity, evidence export, SHA-256, and manifest evidence;
- source accounts and stable Conversation, Node, Message, Revision, and Attachment identities;
- per-export Conversation, Mapping Node, Message, and Attachment observations;
- `message:null` structural nodes, parent/children order, branch topology, current-node evidence;
- raw and normalized timestamps with parse status and provenance;
- source-native ZIP members and RFC 6901 locators;
- parser identity/version and deterministic parser replay;
- import jobs, durable batch commits, idempotency, retry, crash consistency, lease fencing;
- upload, prepared-object, attempt, quarantine, and bounded staging lifecycle;
- evidence-level reconstruction and the foundations required for backup and rebuild.

## Gateway does not own

The Gateway does not own Memory Intelligence, automatic memory extraction, user or relationship models (including interpretations such as “清语怎么看文海”), beliefs, hypotheses, Memory Importance, embeddings, semantic memory, knowledge graphs, summaries, the Context Orchestrator, context ranking, current state, heartbeat, thoughts, agenda, proactive messages, voice, avatar, model routing, tools, or any Agent Runtime. Mem0, Graphiti, MemOS, Hermes, OpenClaw, and model providers are external consumers or derived systems.

No convenience field, schema, API, or dependency may be added to the Gateway merely to make one runtime authoritative. New Gateway state must represent source evidence or evidence operations, not an interpretation owned by an upper layer.

## Deletion boundary

Retention cleanup is runtime-data cleanup, not committed-evidence deletion. Its authority is confined to service-owned namespaces beneath `staging/`: uploads, archive-prepared generations, job attempts, quarantine, cleanup queues, and expired orphan/temp data. It has no authority over `archive/objects`, `manifests`, committed database evidence, or attachment objects.

This is a **logical application authority boundary**, verified through the cleanup implementation and tests. Phase 3 does not claim independently enforced filesystem principals, database roles, ACLs, or row-level security that make committed-evidence deletion impossible for the service process. Infrastructure operators must not present the code-level boundary as an OS/DB least-privilege proof; capability isolation is later operational hardening.

Future evidence deletion requires a separately reviewed, explicit operation with authorization, dependency/cascade analysis, immutable audit evidence, backup policy, dry-run visibility, and confirmation of the exact source account/archive scope. Phase 3 implements no such operation.

## Change control

Changes to this boundary require an architecture review that proves the Source-of-Truth hierarchy remains one-way and the Gateway remains runtime-neutral. A runtime outage or deletion must never remove the only copy of Gateway evidence.

Privacy is part of this boundary: public DTOs and logs must not expose database URLs, tokens, server filesystem paths, raw exception strings, tracebacks, lease tokens, or process identifiers. The current Phase 3 review found no concrete disclosure of these protected values. Archive responses already use explicit public DTOs; Import Job responses still project a broad durable report and therefore require producer discipline. Replacing that broad projection with an explicit allow-list is recorded as P3 hardening, and any future report producer requires privacy review.

Source-native ZIP member names and RFC 6901 locators may remain internal Evidence. Any API, UI, export, or log disclosure of those locators must be explicitly necessary, authorized for the requester, and minimized to the purpose.

## Frozen Maintenance Amendment #1

The GET-only, runtime-neutral Evidence Query API defined in
`GATEWAY_FROZEN_MAINTENANCE_AMENDMENT_1.md` is an approved maintenance extension
subject to Stage 0 review. It exposes explicit allow-list projections of already
committed Evidence and creates no new historical authority. It does not reopen
Phase 3, add a reverse authority path, or permit Core to connect to the Gateway
database.

The extension's pagination guarantee is bounded high-water keyset enumeration,
not a database snapshot. Consumers periodically rescan from the first page and
deduplicate immutable observation identifiers so concurrently late commits are
eventually discovered.

## Frozen Maintenance Amendment #2

`GATEWAY_FROZEN_MAINTENANCE_AMENDMENT_2.md` adds read-only
`POST /api/v1/evidence/search`. Retrieval is a PostgreSQL bigram GIN index over
committed message text. The endpoint does not change `evidence-query-v1`, add an
evidence or source table, or open an ingest path.
