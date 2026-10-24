# Backup / Restore / Rebuild Runbook

The v0.1 operator commands are `python -m scripts.maintenance` (`backup`, `restore`, `upgrade`, `verify`), documented in [BACKUP_RESTORE_AND_UPGRADE.md](BACKUP_RESTORE_AND_UPGRADE.md). This runbook remains the Phase 3 sample-acceptance procedure. It is not the Linux production procedure.

This runbook defines the Phase 3 backup unit and an isolated acceptance procedure. It is an operator workflow, not a one-click product feature.

## Backup unit

Back up one mutually consistent set containing:

1. PostgreSQL Evidence DB in custom `pg_dump` format;
2. the committed `archive/objects` tree, including raw imports and resolved attachment bytes;
3. `manifests`;
4. a backup inventory containing timestamp, database name, Alembic revision, application Git commit, PostgreSQL/pg_dump version, archive-root identity, file count/bytes, and SHA-256 of the DB dump and inventory inputs;
5. restore-time configuration metadata, excluding bearer tokens and database passwords.

`staging/` is deliberately excluded. Stop import writers or take the DB snapshot and immutable object-store snapshot under a coordinated operational boundary. For online capture, take the consistent DB dump first and then copy the immutable object tree: filesystem-only supersets are reported as orphans, but a DB-referenced missing object is a failed backup. Objects are immutable, but a backup must not omit objects referenced by the captured DB.

## Safety rules

- Never run restore, migration, pytest, cleanup, or acceptance import against long-term `qingyu`.
- Use newly created, explicitly named databases and directories.
- Print and verify `current_database()` before every mutation.
- Validate the resolved source/destination directories before recursive copy or removal.
- Keep backup media read-only during restore; restore into a separate location.
- Do not place DB URLs, passwords, or bearer tokens in the inventory.

## Backup procedure

1. Record `SELECT current_database()` and `alembic current`.
2. Quiesce new imports and wait for running ImportJobs to reach a known boundary.
3. Run PostgreSQL 16 `pg_dump --format=custom --no-owner --no-privileges --serializable-deferrable`.
4. Copy only `archive/objects` and `manifests` with metadata and verify copy success.
5. Generate the inventory and SHA-256 checksums.
6. Verify the custom dump with `pg_restore --list` and retain the source unchanged.

## Restore acceptance

1. Create a new empty database; verify its name.
2. Restore with `pg_restore --no-owner --no-privileges --exit-on-error --single-transaction`.
3. Copy committed archive objects and manifests to a new empty archive root. Do not copy `staging`.
4. Verify Alembic revision and schema drift.
5. Re-hash every `ArchiveObject.relative_path`; require size and SHA-256 equality.
6. Require a matching manifest for every ArchiveObject.
7. Validate DB foreign keys and the ArchiveObject references held by revisions and attachment observations.
8. Query Conversation, Node, Revision, and Attachment evidence and reconstruct every Sample #001 batch.
9. Compare reconstructed topology and evidence with the source export: 15 conversations, 944 nodes, 929 messages, 15 null structural nodes, 15 roots, 3 branch parents, 18 leaves, 15 current nodes, 941 current-path nodes, 9 resolved referenced bytes, and 3 raw-only blobs.
10. Run validation with no staging directory and without a worker, runtime, memory engine, or model provider.
11. With a dedicated read token, enumerate the restored conversation observations
    and node pages through the Evidence Query API. Require the same immutable
    references, topology, MessageRevision bindings, fingerprints, and attachment
    observation attempts as direct restore validation. Run this only against the
    isolated restored database and archive root.
12. Verify API responses declare `bounded_high_water_enumeration_v1`; exercise a
    fresh-from-first-page rescan and deduplicate by immutable conversation and
    attachment observation IDs. Do not describe this enumeration as a database
    snapshot.
13. Restore `EVIDENCE_CURSOR_SIGNING_KEY` as protected server-only configuration,
    distinct from both bearer tokens. Rotating it intentionally invalidates all
    outstanding Evidence cursors; it does not alter Evidence records.

The repository helper `scripts/phase3_sample_acceptance.py` imports or validates Sample #001 only in allowlisted disposable database names. The final acceptance record must include the exact commands, tool versions, counts, hashes, and cleanup confirmation.

## Failure handling

Any digest mismatch, missing object/manifest, broken DB reference, schema mismatch, or topology mismatch is a restore failure. Preserve the failed restore for diagnosis; do not repair the backup in place and do not downgrade the acceptance criteria.
