# Backup, restore, and upgrade

This is the v0.1 procedure for one Gateway database and its committed archive. Run the commands from `gateway/` with the virtualenv activated:

```bash
python -m scripts.maintenance --help
```

`docs/BACKUP_RESTORE_RUNBOOK.md` is a separate Phase 3 sample-acceptance procedure, including its Windows helper. It is not this procedure. The maintenance commands do not import a sample, do not delete evidence, and do not downgrade the schema.

## What a backup contains

`backup` writes a new directory:

- `database.dump`: PostgreSQL custom-format dump from `pg_dump --format=custom --no-owner --no-privileges --serializable-deferrable`
- `archive/objects/` and `manifests/`: the committed files under the archive root
- `inventory.json`: format `qingyu-gateway-backup-v1`
- `inventory.sha256`: SHA-256 of the exact `inventory.json` bytes

The inventory records the database name, Alembic revision, `pg_dump` version, PostgreSQL server version, Git commit (or `null` when there is no commit to record), and the SHA-256 and size of every copied file, including the dump. It does not record the database URL, the password, bearer tokens, or absolute paths.

The backup directory is mode `0700`. Files inside it are mode `0600`. The directory is created under a temporary name, fsynced, and renamed into place. A crash can leave a sibling named `.<dest>.partial-<random>`. That directory is not a finished backup; delete it.

`staging/` is not copied. Files at the archive root that are outside `archive/objects/` and `manifests/` are not copied. A symlink anywhere in those two trees fails the backup. Every `archive_objects` row must have its regular file, and the file size and SHA-256 must match the row. Extra files under `archive/objects/` are copied and reported on stderr as orphans. `archive_objects` must already exist, so the database is at revision `0002_archive_objects` or later.

This is a logical dump plus a file copy. It is not a copy of the PostgreSQL data directory. Stop import writers before backing up. The dump and the file copy are not one filesystem snapshot; if a referenced file is missing or does not match the row, the backup fails and the destination is not published.

## Safety rules

- Pass `DATABASE_URL` in the environment. `--database-url` exists, but the process list would then show the password. The password is still never placed on the `pg_dump` or `pg_restore` command line; those programs receive `PGPASSWORD` in their own environment.
- The URL must be PostgreSQL, with a host, a port, a user, and a password. Its database name must equal `--expect-database`. After connecting, `current_database()` must equal that name too. A mismatch exits non-zero and does not dump, restore, or migrate.
- Query parameters that can retarget the connection (`dbname`, `database`, `host`, `port`, `user`, `password`, `options`, and the other keys rejected by the script) are refused.
- `restore` refuses `qingyu`, `qingyu_test`, `postgres`, `template0`, and `template1`. It also refuses a target whose name equals `source_database` in the inventory. Restore into a new empty database, check it, then point Gateway at it and restart.
- The restore database must contain no user tables, views, or sequences. The restore archive root must already exist, must be a real directory, and must be empty. Restored archive files are mode `0644`.
- `upgrade` may target the long-term database, including `qingyu`, when `--expect-database` matches. That is the supported production migration. The Phase 3 runbook's warning is about not practicing its sample procedure against that database.
- `downgrade` is not a command. The script will not run `alembic downgrade`. Forward migration is the only schema change it applies. See `docs/Migration_Downgrade_Policy.md`.
- The script does not print `DATABASE_URL`. Connection failures report the exception class, not the URL.

## Backup

```bash
export DATABASE_URL="postgresql+psycopg://qingyu:<password>@127.0.0.1:5432/qingyu"
python -m scripts.maintenance backup \
  --expect-database qingyu \
  --archive-root /var/lib/qingyu/archive \
  --dest /var/backups/qingyu/2026-10-24
```

`--dest` must not already exist. Its parent must exist.

Check a backup without restoring it:

```bash
python -m scripts.maintenance verify --source /var/backups/qingyu/2026-10-24
```

`verify` recomputes the inventory hash and every file hash, rejects unexpected files and symlinks, and runs `pg_restore --list` on the dump. It does not connect to a database.

## Restore

Create a new database and an empty archive directory. The database name must not be a protected name and must not be the name recorded in the backup.

```bash
export DATABASE_URL="postgresql+psycopg://qingyu:<password>@127.0.0.1:5432/qingyu_restore_20261024"
python -m scripts.maintenance restore \
  --expect-database qingyu_restore_20261024 \
  --archive-root /var/lib/qingyu/restore-20261024 \
  --source /var/backups/qingyu/2026-10-24
```

Restore runs `pg_restore --list`, then `pg_restore --dbname <database-name> --no-owner --no-privileges --exit-on-error --single-transaction`. `--dbname` is the database name only, not a URL. It copies only `archive/objects/` and `manifests/`. It then checks that the Alembic revision equals the inventory and that every `archive_objects` file matches the restored row's size and SHA-256. A mismatch exits non-zero and leaves the new database and archive directory in place for diagnosis. It does not modify the backup or the source database.

The inventory hash detects accidental corruption. It is not a signature. An operator who can rewrite the backup can rewrite the hash. Protected database names are still refused.

## Upgrade

```bash
export DATABASE_URL="postgresql+psycopg://qingyu:<password>@127.0.0.1:5432/qingyu"
python -m scripts.maintenance upgrade \
  --expect-database qingyu \
  --archive-root /var/lib/qingyu/archive \
  --backup-dir /var/backups/qingyu/before-upgrade
```

`--backup-dir` is optional. When it is set, `--archive-root` is required, and the backup runs first. If the backup fails, the schema is not changed. The backup records the revision from before the upgrade.

The command reads the single local Alembic head and applies `alembic upgrade head`. The current revision must be that head or an ancestor of it. A database stamped with an unknown revision, a newer revision, or a history that is not a straight line up to head is refused and is not migrated. When the database is already at head, the command exits 0 and prints `already at <revision>`. Running it again does not rewrite evidence rows.

Take a backup before upgrading a database you need to keep, and practice the restore against a disposable database before relying on it.
