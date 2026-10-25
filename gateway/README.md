# Qingyu Memory Gateway

Phase 1 foundation. PostgreSQL 16 is the database. Compose can start the database alone, or the database and the Gateway together.

### Docker (compose)

From `gateway/`, with Docker Compose v2:

```bash
cp .env.example .env
# Set POSTGRES_PASSWORD to a URL-safe value (letters, digits, hyphen, underscore)
# and three different secrets: API_TOKEN, EVIDENCE_READ_API_TOKEN,
# EVIDENCE_CURSOR_SIGNING_KEY.
# Leave DATABASE_URL on 127.0.0.1. That value is for uvicorn on the host.
# The app service builds its own URL with host `db` and does not read ARCHIVE_ROOT
# from .env; archives go to the archive_data volume at /data.
python -c "import secrets; print(secrets.token_urlsafe(32))"
docker compose up -d --build
curl -fsS http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:8000/health/ready
```

Compose waits until PostgreSQL is healthy, runs `alembic upgrade head`, then starts uvicorn. The Gateway is published on `127.0.0.1:8000`. A blank `API_TOKEN`, `EVIDENCE_READ_API_TOKEN`, `EVIDENCE_CURSOR_SIGNING_KEY`, or `POSTGRES_PASSWORD` makes Compose refuse to start. `/health` does not check the database. `/health/ready` returns 200 after `SELECT 1` and 503 when the database does not answer.

Imports do not finish inside the API process. Run the worker in the same image so it sees `/data` and host `db`:

```bash
docker compose run --rm app python -m app.importers.worker_runtime
```

`docker compose up -d db` still starts only PostgreSQL, which is what the host-run steps below use.

### Linux / macOS (bash)

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
# In .env set POSTGRES_PASSWORD and matching DATABASE_URL,
# and generate three different secrets for API_TOKEN, EVIDENCE_READ_API_TOKEN,
# and EVIDENCE_CURSOR_SIGNING_KEY:
python -c "import secrets; print(secrets.token_urlsafe(32))"
docker compose up -d db
alembic upgrade head
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

### Windows (PowerShell)

```powershell
py -3.12 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item .env.example .env
# In .env set POSTGRES_PASSWORD and matching DATABASE_URL,
# and generate three different secrets for API_TOKEN, EVIDENCE_READ_API_TOKEN,
# and EVIDENCE_CURSOR_SIGNING_KEY:
python -c "import secrets; print(secrets.token_urlsafe(32))"
docker compose up -d db
& .\.venv\Scripts\python.exe -m alembic upgrade head
& .\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

> **Note**: For any `APP_ENV` other than `test`, Gateway requires three distinct, non-example secrets (`API_TOKEN`, `EVIDENCE_READ_API_TOKEN`, and `EVIDENCE_CURSOR_SIGNING_KEY`) and `DATABASE_URL`; see `gateway/app/config.py` and `gateway/.env.example`. Startup logs one redacted line (environment, listen address, archive root, database host, and whether each secret is set). It does not log secret values.

## Export

`GET /api/v1/export/conversations/{conversation_id}` (evidence-read token or API token) and `GET /api/v1/export/archive` (API token only) return a zip bundle with a SHA-256 manifest. Both are read-only: they do not update or delete evidence. See [`docs/EXPORT_BUNDLE.md`](docs/EXPORT_BUNDLE.md).

## Rotating access tokens

`API_TOKEN` and `EVIDENCE_READ_API_TOKEN` are the primary tokens. `API_TOKENS` and `EVIDENCE_READ_API_TOKENS` are optional comma-separated tokens that are accepted at the same time. Bearer checks use `secrets.compare_digest` against every configured token.

To rotate one process without a window where the old token is already dead and the new token is not live yet:

1. Set the primary to the new token and put the old token in the matching extra list (or the other way around). Restart so both are active.
2. Move clients to the new token.
3. Remove the old token and restart again.

A single restart still drops in-flight connections. Overlapping two processes behind the proxy, both configured with the old and new tokens, avoids that gap. The evidence-read token cannot call import, archive, or retry routes. The cursor signing key is not a bearer token; rotating it still invalidates outstanding evidence cursors and does not change evidence rows.

Non-upload request bodies are limited to `REQUEST_BODY_MAX_BYTES` (default 1 MiB). Import and archive uploads keep `IMPORT_UPLOAD_MAX_BYTES` plus 1 MiB of multipart overhead. A repeated, negative, or non-integer `Content-Length` is rejected. The process also counts requests in a fixed window (`RATE_LIMIT_REQUESTS` per `RATE_LIMIT_WINDOW_SECONDS`, default 600 per 60 seconds) per client address. `/health` and `/health/ready` are not counted. The window is per process. `TRUSTED_PROXIES` is empty by default, so `X-Forwarded-For` and `X-Real-IP` are ignored and the key is the direct peer. How to set it behind a proxy is in [../docs/DEPLOYMENT.md](../docs/DEPLOYMENT.md).

`LOG_FORMAT` is `text` or `json` (`LOG_LEVEL` selects the level). Each response echoes `X-Request-ID`, which is the caller's value when that header is a single short token and a generated id otherwise. Access logs record the method, path, status, duration, request id, and direct peer. They do not record the query string, headers, tokens, or bodies. `GET /metrics` is off unless `METRICS_ENABLED=true`. It exposes per-process request count and latency by route template and status. `METRICS_TOKEN`, when set, is a separate bearer token for that endpoint. See [../docs/DEPLOYMENT.md](../docs/DEPLOYMENT.md).

## Backup, restore, and upgrade

From this directory, `python -m scripts.maintenance` takes a logical backup, restores it into a new database, or applies forward-only Alembic upgrades. `staging/` is not part of a backup, and downgrade is not a supported operation. See [docs/BACKUP_RESTORE_AND_UPGRADE.md](docs/BACKUP_RESTORE_AND_UPGRADE.md). [docs/BACKUP_RESTORE_RUNBOOK.md](docs/BACKUP_RESTORE_RUNBOOK.md) is the separate Phase 3 sample-acceptance procedure.

Deployment of this process, the import worker, PostgreSQL, and the archive directory is described in [../docs/DEPLOYMENT.md](../docs/DEPLOYMENT.md).

The import pipeline and parser contract are specified in [../docs/IMPORTER_SPEC.md](../docs/IMPORTER_SPEC.md).

## Running tests

Gateway tests require a dedicated test database named `qingyu_test` with migrations applied to the latest revision. If the database isn't migrated, the tests stop early and tell you to run `alembic upgrade head` against it.

Windows (PowerShell):

```powershell
$env:APP_ENV="test"
$env:TEST_DATABASE_URL="postgresql+psycopg://qingyu:<your-password>@127.0.0.1:5432/qingyu_test"
$env:DATABASE_URL=$env:TEST_DATABASE_URL
& .\.venv\Scripts\python.exe -m alembic upgrade head
& .\.venv\Scripts\python.exe -m pytest -q
```

Linux / macOS (bash), from `gateway/` with its venv activated:

```bash
. .venv/bin/activate
export APP_ENV=test
export TEST_DATABASE_URL="postgresql+psycopg://qingyu:<your-password>@127.0.0.1:5432/qingyu_test"
export DATABASE_URL="$TEST_DATABASE_URL"
alembic upgrade head
python -m pytest -q
```
