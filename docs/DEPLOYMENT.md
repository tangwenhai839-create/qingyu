# Deploying the Gateway

This guide is for the Gateway in `gateway/`: the FastAPI process, the import worker, and PostgreSQL 16. Core is a separate experiment and is not part of this deployment. The repository does not ship a Gateway container image, a Compose service for the application, or a TLS listener. Those are not described here as if they existed.

The project status in the root README still applies. This page only describes how to run the processes and defaults that are in the tree today.

## Processes

Use Python 3.12. From `gateway/`, create a virtualenv and install the package. `pip install -e .` is enough to run it. `pip install -e ".[dev]"` adds the test tools.

Start PostgreSQL from `gateway/`:

```bash
docker compose up -d db
```

`gateway/docker-compose.yml` defines one service, `db`, using the `postgres:16` image. It creates database `qingyu` and user `qingyu`. `POSTGRES_PASSWORD` must be set or Compose refuses to start. The published port is `127.0.0.1:5432:5432`. The named volume `qingyu_postgres` is mounted at `/var/lib/postgresql/data` inside the container. Compose does not build or start the Gateway.

Apply the schema from `gateway/`, with `DATABASE_URL` pointing at that database:

```bash
alembic upgrade head
```

`alembic upgrade head` is the supported schema change. Downgrades are not the production path; see `gateway/docs/Migration_Downgrade_Policy.md`.

Start the API from `gateway/` so it reads `gateway/.env`:

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

`HOST` and `PORT` in `.env.example` are settings fields. No Gateway code reads them. The listen address is the `--host` and `--port` you pass to uvicorn. Keep that on `127.0.0.1` unless you have another way to stop the port being reachable from the network.

Imports do not finish inside the API process. A second process, started from the same directory and the same environment, claims jobs and deletes expired staging:

```bash
python -m app.importers.worker_runtime
```

Its defaults are `IMPORT_WORKER_POLL_SECONDS=1`, `IMPORT_WORKER_HEARTBEAT_SECONDS=10`, and `IMPORT_WORKER_LEASE_SECONDS=30`. Staging retention uses `STAGING_FAILED_RETENTION_DAYS` (7), `STAGING_CLEANUP_INTERVAL_SECONDS` (300), and `STAGING_CLEANUP_MAX_CANDIDATES` (100). Settings are read when each process starts. Changing `.env` has no effect until that process is restarted.

`GET /health` does not require a token. It is not an authorization boundary.

## Volumes

Two directories have to survive a restart.

The PostgreSQL volume is `qingyu_postgres`. Losing it loses the database unless you have a dump.

`ARCHIVE_ROOT` is a directory on the host. It is not a Compose volume. `.env.example` contains the placeholder `<repo>/data`; replace it with a real writable directory. The Gateway uses three trees under that directory:

- `archive/objects/` — content-addressed bytes
- `manifests/` — manifest files for those objects
- `staging/` — uploads and other temporary import files

`staging/` is only for in-progress import work. The worker may delete files there. It is not a copy of the evidence. The cleanup code is not allowed to delete `archive/objects/`, `manifests/`, or committed database rows. That is an application rule, not a separate database role or filesystem ACL. The user that runs uvicorn and the worker can still delete those files. Limit that account to the people who operate the Gateway.

## Secrets

`.env` is gitignored. Start from `gateway/.env.example`.

`POSTGRES_PASSWORD` is the database password. `DATABASE_URL` must use the same password. Compose does not expand variables inside `DATABASE_URL`, so write the password in both places. If the password contains `@`, `:`, `/`, or `%`, percent-encode it in `DATABASE_URL`.

For any `APP_ENV` other than `test`, startup requires three different values that are not empty and not one of `changeme`, `example`, `change-this-local-token`, or `change-me`:

- `API_TOKEN` — bearer token for import, archive, and other operator routes
- `EVIDENCE_READ_API_TOKEN` — bearer token for `GET /api/v1/evidence/...` and `GET /api/v1/export/conversations/{id}` only. It cannot call import, archive, the full archive export, or `/api/v1/protected-ping`. The operator token can also read and export evidence.
- `EVIDENCE_CURSOR_SIGNING_KEY` — HMAC key for evidence cursors. It is not accepted as a bearer token. Replacing it makes existing cursors fail and does not change evidence rows.

Bearer checks use `secrets.compare_digest` against every active token. `API_TOKENS` and `EVIDENCE_READ_API_TOKENS` are optional comma-separated extra tokens accepted alongside the primaries, so a token can be rotated by overlap; see “Rotating access tokens” in `gateway/README.md`. Do not run a real process with `APP_ENV=test`; that setting skips these checks. Generate values with `python -c "import secrets; print(secrets.token_urlsafe(32))"`.

The Gateway test suite will not run against a database whose name is not `qingyu_test`. Do not point `TEST_DATABASE_URL` at the long-term `qingyu` database.

## Reverse proxy and TLS

The Gateway process speaks HTTP. It does not terminate TLS. Put the proxy on the same machine and let it connect to `127.0.0.1:8000`. Do not publish port 8000 or port 5432 on a public address. The Compose file already limits PostgreSQL to `127.0.0.1`.

The repository does not contain a proxy configuration. The block below is an example of a config you would write yourself. The certificate and key are files you install. Nginx's default request body limit is 1 MiB, which is smaller than an import, so the example raises it.

```nginx
server {
    listen 443 ssl;
    server_name memory.example.com;
    ssl_certificate     /etc/ssl/certs/memory.example.com.crt;
    ssl_certificate_key /etc/ssl/private/memory.example.com.key;
    client_max_body_size 513m;
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

`POST /api/v1/import/chatgpt` and `POST /api/v1/archive/objects` are limited to `IMPORT_UPLOAD_MAX_BYTES` plus 1 MiB of multipart overhead. The default `IMPORT_UPLOAD_MAX_BYTES` is 536870912, so the middleware limit is 537919488 bytes. A body above that returns 413. Every other request body is limited to `REQUEST_BODY_MAX_BYTES` (default 1 MiB), and a repeated, negative, or non-integer `Content-Length` returns 400.

The process also applies an in-process fixed-window rate limit (`RATE_LIMIT_REQUESTS` per `RATE_LIMIT_WINDOW_SECONDS`, default 600 per 60 seconds). `/health` is exempt. The window is per process and is not shared across processes. With `TRUSTED_PROXIES` empty, which is the default, the key is the direct TCP peer. `X-Forwarded-For` and `X-Real-IP` are ignored, so a client cannot choose a bucket by sending those headers, and every client behind one proxy shares that proxy's bucket.

Set `TRUSTED_PROXIES` to the CIDRs of proxies you run when each client should have its own bucket. For the nginx example above the peer is the local proxy, so `TRUSTED_PROXIES=127.0.0.1/32,::1/128`. A bare IP is a single host (`/32` or `/128`). Entries must be the network address (`10.0.0.0/8`, not `10.1.2.3/8`). A blank entry or a value that is not a CIDR or IP prevents the process from starting.

The header is read only when the direct peer belongs to that list. A spoofed header from any other peer is ignored and the key stays the peer address. When the peer is trusted:

- `X-Forwarded-For` is the hop list, client first. The limiter walks from the right, skips addresses that are themselves in `TRUSTED_PROXIES`, and uses the rightmost hop that is not. Addresses further left are ignored, which is what stops a client from prepending a fake address. `$proxy_add_x_forwarded_for` appends the address that connected to nginx, so the real client is that rightmost untrusted hop.
- If `X-Forwarded-For` is absent, `X-Real-IP` is used as that single client address.
- A hop that is not an IP does not create a bucket. The key falls back to the direct peer. IPv4-mapped IPv6 (`::ffff:203.0.113.5`) is the same key as `203.0.113.5`.

List only hops you operate. Trusting `0.0.0.0/0` or a network that clients can connect from lets those clients supply the header themselves. If several proxies sit in front of the process, list each of their CIDRs so the walk skips them and stops at the client. The bucket is still the address, not the port.

## Logging and metrics

`LOG_LEVEL` is the standard library level (`INFO` by default). An unknown level falls back to `INFO`. `LOG_FORMAT` is `text` or `json`. `text` is the default. `json` writes one JSON object per line with `timestamp`, `level`, `logger`, `message`, and `request_id`, plus any extra fields the record already carries. Anything other than those two formats stops the process at startup. The import worker reads the same two settings when it starts.

Every HTTP response carries `X-Request-ID`. A single incoming value of 1–128 characters from `[A-Za-z0-9._-]` is echoed. A missing, repeated, or otherwise unusable value is replaced with a new id, so a client cannot put a line break or a space into the log. The same id is included on log records emitted while the request is handled, and on the `request_id` field of the JSON error bodies the API already returns.

Access logs are one line per request from the logger `qingyu.access`: method, path, status, duration, request id, and the direct peer address. The path has no query string. Headers, bearer tokens, and request or response bodies are not written. `/health` is included.

`GET /metrics` returns Prometheus text (`text/plain; version=0.0.4`). It is not in the OpenAPI schema and it responds 404 until `METRICS_ENABLED=true`. The series are `qingyu_http_requests_total` and `qingyu_http_request_duration_seconds` (a histogram), both labeled with the route template and the status code. Unmatched paths use the label `unmatched`, so a scan of random URLs does not create a series per path. The numbers belong to this process only; a second worker or a second uvicorn process has its own.

`METRICS_TOKEN`, when non-empty, requires `Authorization: Bearer <METRICS_TOKEN>`. The operator API token is not accepted unless you set `METRICS_TOKEN` to that same value. A scraper credential therefore does not grant import or archive access. When `METRICS_TOKEN` is empty and metrics are enabled, anyone who can reach the port can read the counters. Leave the process on `127.0.0.1` or set a token. The token is not written to the access log or the metrics body.

## Backups

Back up three things together: a logical dump of database `qingyu`, `archive/objects/`, and `manifests/`. Do not treat `staging/` or a file copy of the running PostgreSQL data directory as that backup.

The supported v0.1 tool is `python -m scripts.maintenance` (run from `gateway/` with `DATABASE_URL` in the environment): `backup`, `verify`, `restore` (only into a new empty database and archive root), and `upgrade` (forward-only, optionally taking a backup first). See `gateway/docs/BACKUP_RESTORE_AND_UPGRADE.md`.

The Phase 3 sample-acceptance procedure is separate:

`gateway/docs/BACKUP_RESTORE_RUNBOOK.md` is the written procedure. It uses PostgreSQL 16 `pg_dump --format=custom --no-owner --no-privileges --serializable-deferrable`, then a copy of `archive/objects` and `manifests`. Restore uses `pg_restore --no-owner --no-privileges --exit-on-error --single-transaction` into a new database, then checks file sizes and SHA-256 values against `archive_objects`. The inventory it describes must not contain database URLs, passwords, or bearer tokens.

The script `gateway/scripts/phase3_backup_restore_acceptance.ps1` is the Windows helper for that Phase 3 sample acceptance. It is not a general Linux installer. Do not practice a restore against the long-term `qingyu` database; the runbook says to use a new database name.

There is no evidence-delete API. A restore is a new copy. It does not edit the source rows.

## Defaults the process already applies

- Compose publishes PostgreSQL only on `127.0.0.1`.
- The documented uvicorn command listens on `127.0.0.1:8000`.
- Non-test startup rejects missing, example, or identical secrets. Comparisons use `secrets.compare_digest`.
- SQLAlchemy is created with `hide_parameters=True`. Database failures returned to clients use an error code and a request id.
- Evidence and export HTTP responses set `Cache-Control: private, no-store`.
- Logs are text unless `LOG_FORMAT=json`. Responses echo `X-Request-ID`. Access logs omit the query string, headers, tokens, and bodies.
- `GET /metrics` is disabled unless `METRICS_ENABLED=true`, and `METRICS_TOKEN` can require a bearer token that is separate from `API_TOKEN`.
- The request-limit middleware caps the two upload routes and every other request body, and rate-limits per client address. `TRUSTED_PROXIES` decides whether that address is the direct peer or the client named by a trusted proxy.
- Staging cleanup does not delete committed archive objects, manifests, or evidence rows.
- The OpenAPI UI is served by the same process at `/docs`. It is reachable by anyone who can reach the port. Keeping the process on `127.0.0.1`, behind the proxy, is what limits that.
