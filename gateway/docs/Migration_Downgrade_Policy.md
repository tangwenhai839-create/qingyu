# Migration downgrade policy

Forward migrations are the supported production path. Downgrades are intended
for disposable development and migration-verification databases and can be
destructive when a revision drops tables or columns.

Migration `0003_archive_observations` moves source provenance out of
`archive_objects`. Its historical downgrade drops the observation table and does
not reconstruct provenance or restore the original non-null source columns. It
must therefore be treated as a destructive, unsupported production downgrade.
The published historical migration is intentionally unchanged.

Migration `0013_import_job_defaults` has a deterministic downgrade to
the nullable/default contract established by `0005` and `0006`. It does not
attempt to repair older destructive downgrade semantics.

Migration `0017_evidence_search_bigram` adds `public.qingyu_text_bigrams(text)` and a GIN index on `message_revisions.content_text`. Upgrade builds the index with `CREATE INDEX CONCURRENTLY`. Downgrade drops the index and the function in the migration transaction and does not delete message text. It is only for disposable databases.

Migration `0018_ingest_requests` adds the `ingest_requests` idempotency ledger. Downgrade drops that table. It does not delete archive objects, import jobs, or committed evidence. It is only for disposable databases.

Migration `0015_attachment_observations` moves runtime attachment evidence into
an immutable, attempt-scoped table. Its downgrade deterministically restores the
legacy attachment columns from the latest observation for each attachment, but
necessarily collapses multi-attempt history. The downgrade is therefore only
supported for disposable development and migration-verification databases.
