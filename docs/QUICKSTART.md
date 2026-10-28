# Quickstart: import a ChatGPT export and query it

This walks a new Gateway from clone to a ChatGPT export you can query. Every curl talks to `http://127.0.0.1:8000`. The paths, status codes, and JSON shapes below are the ones implemented in `gateway/app` (`api_import.py`, `api_evidence.py`, `evidence/schemas.py`, `importers/status.py`). Host binding, TLS, volumes, and backups are in [DEPLOYMENT.md](DEPLOYMENT.md). Windows commands and token rotation are in [gateway/README.md](../gateway/README.md).

You need Python 3.12, PostgreSQL 16, and a ChatGPT data-export zip whose file name ends in `.zip` and that contains `conversations.json`. The default database path is Docker Compose v2 (`docker compose up -d db` below).

## 1. Clone, configure, migrate, and start

From a shell:

```bash
git clone https://github.com/tangwenhai839-create/qingyu.git
cd qingyu/gateway
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Edit `gateway/.env` before the processes start:

- `POSTGRES_PASSWORD`, and the same password in the host `DATABASE_URL` (`postgresql+psycopg://qingyu:<password>@127.0.0.1:5432/qingyu`). Percent-encode `@`, `:`, `/`, and `%` in that URL.
- `API_TOKEN` — operator bearer token. Import and retry routes accept this token (or an entry in `API_TOKENS`).
- `EVIDENCE_READ_API_TOKEN` — read bearer token for `GET /api/v1/evidence/...`. The operator token is also accepted there. Import routes reject the read token.
- `EVIDENCE_CURSOR_SIGNING_KEY` — HMAC key for evidence cursors. It is not a bearer token.
- `ARCHIVE_ROOT` — a writable absolute directory. Replace the `<repo>/data` placeholder. The API and the worker must use the same directory.

Outside `APP_ENV=test`, startup requires `DATABASE_URL`. `API_TOKEN`, `EVIDENCE_READ_API_TOKEN`, and `EVIDENCE_CURSOR_SIGNING_KEY` must differ from each other and from the example placeholders (`gateway/app/config.py`).

Start PostgreSQL, apply the schema, then start the API. Run these from `gateway/` so Alembic and the app read `gateway/.env`:

```bash
docker compose up -d db
alembic upgrade head
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

`alembic upgrade head` is required before the first request that uses the database. uvicorn binds the `--host` and `--port` on the command line. `HOST` and `PORT` in `.env` are recorded in settings and the startup log.

`docker compose up -d db` is the default path to PostgreSQL 16. If Docker is unavailable, any PostgreSQL 16 works: point `DATABASE_URL` at that server (host, port, user, database, and password), for example `postgresql+psycopg://<user>:<password>@<host>:<port>/<db>`, then run `alembic upgrade head` and uvicorn the same way. Skip `docker compose up -d db` in that case.

Imports do not finish inside the API process. `gateway/docker-compose.yml` defines `db` and `app` only. It has no worker service, so Compose does not start one. The worker must run as a separate process. In a second terminal, from the same `gateway/` directory and virtualenv, start it. It claims `pending` jobs and deletes expired staging:

```bash
cd /path/to/qingyu/gateway
. .venv/bin/activate
python -m app.importers.worker_runtime
```

Leave both processes running. A job stays `pending` until the worker claims it.

The Gateway process reads `gateway/.env` itself. The curls below read tokens from the shell, so export the same values:

```bash
export API_TOKEN='the API_TOKEN value from gateway/.env'
export EVIDENCE_READ_API_TOKEN='the EVIDENCE_READ_API_TOKEN value from gateway/.env'
```

Check that the API is up. Neither health route needs a token. `/health` does not touch the database. `/health/ready` returns 200 after `SELECT 1` and 503 when the database does not answer.

```bash
curl -sS http://127.0.0.1:8000/health
curl -sS http://127.0.0.1:8000/health/ready
curl -sS -H "Authorization: Bearer $API_TOKEN" \
  http://127.0.0.1:8000/api/v1/protected-ping
```

`GET /health` with the default `APP_NAME`:

```json
{"status": "ok", "service": "Qingyu Memory Gateway", "version": "0.1.0"}
```

`GET /health/ready` when PostgreSQL answers:

```json
{"status": "ready"}
```

`GET /api/v1/protected-ping` with the operator token:

```json
{"status": "ok"}
```

A missing or wrong operator token on that route is 401 with `{"detail": "Invalid or missing bearer token"}`.

### Docker Compose instead of a host process

From `gateway/`, with the same `.env` filled in, `docker compose up -d --build` waits for PostgreSQL, runs `alembic upgrade head` inside the app container, and publishes the API on `127.0.0.1:8000`. Leave `DATABASE_URL` on `127.0.0.1`; the app service builds its own URL. `POSTGRES_PASSWORD` must be URL-safe for that service (letters, digits, hyphen, underscore). Compose has no worker service, so the import worker is still a separate process. It has to see the same archive volume:

```bash
docker compose run --rm app python -m app.importers.worker_runtime
```

The curls in the next sections are the same.

## 2. Upload a ChatGPT export

The worker must be running as its own process before this upload. Compose has no worker service (`db` and `app` only), so neither `docker compose up -d db` nor `docker compose up -d --build` starts it. From `gateway/`, with the same virtualenv and `.env` as the API:

```bash
python -m app.importers.worker_runtime
```

When the API is the Compose `app` service, run the same module in that image so it sees the archive volume:

```bash
docker compose run --rm app python -m app.importers.worker_runtime
```

Leave that process running. The 202 below stays `pending` until the worker claims the job.

`POST /api/v1/import/chatgpt` accepts the zip as multipart field `file` and returns 202. The router is mounted at `/api/v1` and depends on `require_token`, so the call needs the operator bearer token. `EVIDENCE_READ_API_TOKEN` is not enough.

```bash
import_body=$(curl -sS -X POST http://127.0.0.1:8000/api/v1/import/chatgpt \
  -H "Authorization: Bearer $API_TOKEN" \
  -F "file=@chatgpt-export.zip")
printf '%s\n' "$import_body"
```

The handler returns one field, `import_job_id`. The 202 body does not contain `job_id`.

```json
{"import_job_id": "3f9c2a54-8f21-4d1e-9c66-3f6e88a1b2d4"}
```

The file name must end in `.zip` (415 otherwise). The zip is rejected with 413 when it exceeds `IMPORT_UPLOAD_MAX_BYTES` (default 536870912). The request-size middleware applies that limit plus 1 MiB of multipart overhead. Optional form field `source_platform` defaults to `chatgpt`.

## 3. Poll the import job

`GET /api/v1/import-jobs/{job_id}` uses the same operator token. `{job_id}` is only the path parameter name. The value is the `import_job_id` field from the step 2 202 body. That body has no `job_id` field. A poll script must read `import_job_id`, not `job_id`.

```bash
# 202 JSON key is import_job_id, not job_id.
import_job_id=$(printf '%s\n' "$import_body" | python -c 'import json,sys; print(json.load(sys.stdin)["import_job_id"])')
curl -sS -H "Authorization: Bearer $API_TOKEN" \
  "http://127.0.0.1:8000/api/v1/import-jobs/${import_job_id}"
```

The sample responses below use `import_job_id` `3f9c2a54-8f21-4d1e-9c66-3f6e88a1b2d4`. The same GET with that id:

```bash
curl -sS -H "Authorization: Bearer $API_TOKEN" \
  http://127.0.0.1:8000/api/v1/import-jobs/3f9c2a54-8f21-4d1e-9c66-3f6e88a1b2d4
```

An unknown id is 404 with `{"detail": "Import Job not found"}`.

Right after a 202, before the worker claims the job, the handler returns the stored row. `progress` is the checkpoint object (`{}` until a batch commits). `errors` is `error_summary`: a string, or `null`. `report` is `report_json`, an object (`{}` until the worker finishes).

```json
{
  "import_job_id": "3f9c2a54-8f21-4d1e-9c66-3f6e88a1b2d4",
  "status": "pending",
  "result_code": null,
  "error_code": null,
  "progress": {},
  "counts": {"discovered": 0, "imported": 0, "skipped": 0, "failed": 0},
  "errors": null,
  "report": {}
}
```

Poll until `status` is terminal. From `gateway/app/importers/status.py` the non-terminal statuses are `pending` and `running`. The terminal statuses are `completed`, `failed`, `partial`, and `cancelled`. A successful ChatGPT import finishes as `completed` with `result_code` `imported`.

The sample below is one conversation with two new messages in a zip whose only member is `conversations.json`. That is the shape `mark_completed` stores: `counts` are message tallies (`imported` is `report.messages.new`), `progress` is the checkpoint written with the durable batch (`committed_batches` is the 1-based sequence, `last_batch_key` is `{zip member}:conversation:{index}`), and `report` is the ledger summary. The summary starts from `empty_report`, then adds `scan` from the zip extract (`file_count`, `total_uncompressed_bytes`, and the zip `sha256`), then `items`, `imported_messages`, `imported_conversations`, `durable_batches`, and `durable_counts`. `errors` stays `null` on success. `report.errors` is a separate list of diagnostic objects. An official export that also contains other files adds `{"type": "unparsed_archive_members", "severity": "info"}` to `report.unknown`; the `count` on that processor warning is not kept.

```json
{
  "import_job_id": "3f9c2a54-8f21-4d1e-9c66-3f6e88a1b2d4",
  "status": "completed",
  "result_code": "imported",
  "error_code": null,
  "progress": {
    "committed_batches": 1,
    "last_batch_key": "conversations.json:conversation:0"
  },
  "counts": {"discovered": 2, "imported": 2, "skipped": 0, "failed": 0},
  "errors": null,
  "report": {
    "parser": {"name": "chatgpt-export", "version": "1", "detection": "matched"},
    "conversations": {"new": 1, "existing": 0, "changed": 0, "skipped": 0, "failed": 0},
    "messages": {"new": 2, "existing": 0, "changed": 0, "skipped": 0, "failed": 0},
    "revisions": {"new": 2, "existing": 0},
    "attachments": {"available": 0, "missing": 0, "referenced_only": 0, "failed": 0},
    "tree_anomalies": [],
    "unknown": [],
    "errors": [],
    "scan": {
      "file_count": 1,
      "total_uncompressed_bytes": 128,
      "sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
    },
    "items": {"conversations": 1, "messages": 2},
    "imported_messages": 2,
    "imported_conversations": 1,
    "durable_batches": 1,
    "durable_counts": {
      "discovered_count": 2,
      "imported_count": 2,
      "skipped_count": 0,
      "failed_count": 0
    }
  }
}
```

A failed job sets `status` to `failed` (or `partial` when a durable batch already committed), sets `result_code` and `error_code`, and sets `errors` to that error string. A zip the parser does not recognize finishes `failed` with `result_code` `unsupported_structure`.

`POST /api/v1/import-jobs/{job_id}/retry` also needs the operator token. The path value is the same `import_job_id` from the 202 body, not a `job_id` field. It applies only when `status` is `failed` or `partial`. A retry the worker accepts returns 202:

```bash
curl -sS -X POST \
  -H "Authorization: Bearer $API_TOKEN" \
  http://127.0.0.1:8000/api/v1/import-jobs/3f9c2a54-8f21-4d1e-9c66-3f6e88a1b2d4/retry
```

```json
{"import_job_id": "3f9c2a54-8f21-4d1e-9c66-3f6e88a1b2d4", "status": "pending"}
```

Any other status, and a `failed` or `partial` job the worker will not replay, returns 409. Poll the same GET after a retry; the worker has to be running for the new attempt to leave `pending`.

## 4. Query the conversation observations

`GET /api/v1/evidence/conversation-observations` accepts the operator token or `EVIDENCE_READ_API_TOKEN`. The read token is limited to evidence reads and conversation export. Import, archive, and retry stay on `API_TOKEN`.

```bash
curl -sS -H "Authorization: Bearer $EVIDENCE_READ_API_TOKEN" \
  "http://127.0.0.1:8000/api/v1/evidence/conversation-observations?limit=5"
```

The response model is `ObservationPage` (`extra` is forbidden). Its fields are `contract_version`, `meaning`, `consistency`, `items`, `next_cursor`, and `limit`. There is no top-level `kind`. `kind: "conversation-observations"` is only inside the signed cursor payload that `next_cursor` carries. Each element of `items` is a `ConversationObservationSummary`. The title field is `observed_title`. The source timestamp field is `normalized_created_at`.

`limit` defaults to 50 and must be from 1 to 100. Optional query parameters are `source_account_id`, `conversation_id`, and `cursor`. Send the same filters with `cursor` that you used to obtain it. The response sets `Cache-Control: private, no-store`.

This sample is one row with illustrative ids. It validates as `ObservationPage` / `ConversationObservationSummary`:

```json
{
  "contract_version": "evidence-query-v1",
  "meaning": "gateway_recorded_historical_evidence",
  "consistency": "bounded_high_water_enumeration_v1",
  "items": [
    {
      "conversation_observation_id": "8f0a1b2c-3d4e-4f50-a6b7-8c9d0e1f2a3b",
      "import_batch_commit_id": "11111111-2222-4333-8444-555555555555",
      "conversation_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
      "source_account_id": "1a2b3c4d-5e6f-4780-91a2-b3c4d5e6f708",
      "archive_object_id": "0f1e2d3c-4b5a-4697-8879-6a5b4c3d2e1f",
      "source_platform": "chatgpt",
      "parser_name": "chatgpt-export",
      "parser_contract_version": "1",
      "committed_at": "2026-09-14T10:22:31Z",
      "observed_at": "2026-09-14T10:22:31Z",
      "observed_title": "Discussion about caching strategies",
      "normalized_created_at": "2026-09-14T10:22:31Z",
      "normalized_updated_at": null,
      "create_time_parse_status": "parsed",
      "update_time_parse_status": "parsed",
      "current_conversation_node_id": "9c8b7a6d-5e4f-4321-80ab-cdef01234567",
      "current_node_status": "resolved",
      "source_observation_fingerprint": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "canonical_fragment_fingerprint": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
      "fragment_fingerprint_version": "canonical-json-v1"
    }
  ],
  "next_cursor": null,
  "limit": 5
}
```

A missing or wrong bearer token is 401 with `{"detail": {"code": "authentication_required"}}`. A bad cursor is 400 with `{"detail": {"code": "evidence_cursor_invalid"}}`.

When `next_cursor` is a string, request the next page with that value and the same filters:

```bash
curl -sS -H "Authorization: Bearer $EVIDENCE_READ_API_TOKEN" \
  "http://127.0.0.1:8000/api/v1/evidence/conversation-observations?limit=5&cursor=PASTE_NEXT_CURSOR"
```

Open `http://127.0.0.1:8000/docs` for the OpenAPI UI served by the same process.
