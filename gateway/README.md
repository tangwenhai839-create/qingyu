# Qingyu Memory Gateway

Phase 1 foundation. PostgreSQL 16 runs only in Docker Compose.

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

> **Note**: For any `APP_ENV` other than `test`, Gateway requires three distinct, non-example secrets (`API_TOKEN`, `EVIDENCE_READ_API_TOKEN`, and `EVIDENCE_CURSOR_SIGNING_KEY`); see `gateway/app/config.py`.

## Backup, restore, and upgrade

From this directory, `python -m scripts.maintenance` takes a logical backup, restores it into a new database, or applies forward-only Alembic upgrades. `staging/` is not part of a backup, and downgrade is not a supported operation. See [docs/BACKUP_RESTORE_AND_UPGRADE.md](docs/BACKUP_RESTORE_AND_UPGRADE.md). [docs/BACKUP_RESTORE_RUNBOOK.md](docs/BACKUP_RESTORE_RUNBOOK.md) is the separate Phase 3 sample-acceptance procedure.

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
