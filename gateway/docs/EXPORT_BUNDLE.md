# Export bundle v1

The Gateway can package committed evidence into a zip bundle whose SHA-256
manifest a caller can check without trusting the transport. Export is a read
of evidence that already exists. It does not insert, update, or delete
conversations, messages, observations, archive objects, or manifests.

## Routes

| Method and path | Credential | Body |
|---|---|---|
| `GET /api/v1/export/conversations/{conversation_id}` | Evidence-read token or API token | One conversation |
| `GET /api/v1/export/archive` | API token only | Every committed observation and every raw archive object |

There is no POST, PUT, PATCH, or DELETE export route.

Both responses are `application/zip` with `Cache-Control: private, no-store`.
`X-Qingyu-Export-Format` is `qingyu-export-bundle-v1`. `X-Qingyu-Manifest-SHA256`
is the SHA-256 of the exact `manifest.json` bytes stored in the zip.

A missing conversation is `404` with `{"detail":{"code":"conversation_not_found"}}`.
A digest mismatch, missing object, symlink, or unverified archive object fails
the archive export with `409` and `{"detail":{"code":"export_integrity_failure"}}`.
The database is left unchanged.

## What a bundle contains

`manifest.json` is canonical JSON (sorted keys, UTF-8, trailing newline). It
records `format`, `scope` (`conversation` or `archive`), `conversation_id`,
`generated_at`, `evidence_contract` (`evidence-query-v1`), `consistency`
(`bounded_high_water_enumeration_v1`), `high_water`, and `files`. Each file
entry has `path`, `sha256`, and `size_bytes`. The manifest does not list
itself.

Evidence files use the same public fields as the Evidence Query API:

- `evidence/conversation-observations.json`
- `evidence/observations/{observation_id}/nodes.json`
- `evidence/observations/{observation_id}/attachments/{attachment_id}.json` when that observation has attachments

The observation set is the bounded high-water enumeration used by the query
API, paged inside one read-only transaction. It is not a database snapshot.
Nodes and attachment attempts belong to observations already inside that
enumeration.

Conversation scope also writes `archive/referenced-objects.json`: `object_id`,
`sha256`, `size_bytes`, and `media_type` for objects referenced by that
conversation. It does not include raw bytes. The evidence-read token therefore
does not gain the archive files.

Archive scope writes `objects/{sha256}` (the raw bytes) and
`objects/{sha256}.json` (the same public metadata) for every `ArchiveObject`.
Bytes are hashed while they are copied into the zip and must match the stored
digest and size. Staging is not walked and is not part of the bundle.

Bundles omit server filesystem paths, database URLs, bearer tokens, raw JSON
pointers, and the on-disk per-object manifest files (those manifests contain
storage paths).

## Checking a bundle

1. Read `manifest.json` from the zip and compute its SHA-256. It must equal `X-Qingyu-Manifest-SHA256`.
2. The other zip members must be exactly the manifest's `files` list.
3. Each member's uncompressed SHA-256 and size must equal its manifest entry.
4. Reject a bundle whose `format` is not `qingyu-export-bundle-v1`.

`generated_at` changes on each request, so two exports of unchanged evidence
match on the evidence and object digests even when the manifest digest differs.
The same `generated_at` produces the same manifest bytes.

## Append-only boundary

The export service's first statement is `SET TRANSACTION READ ONLY`, and ORM
insert, update, and delete statements are rejected. Export does not publish
archive objects, does not refresh integrity timestamps, and does not clean
staging. Repeating an export does not create a new evidence revision.
