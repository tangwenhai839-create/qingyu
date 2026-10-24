# Qingyu Core

Qingyu Core is the independent identity, continuity, governance and audit
foundation for the Qingyu system. It is deliberately separate from the
Gateway repository and database.

The repository contains no User Model, Relationship Model, Memory Intelligence,
Local Journal, Runtime Event ingest, Hermes integration, or model provider code.

## Boundaries

- Core uses its own PostgreSQL database, owner role and Alembic history.
- Gateway Evidence is consumed only through the GET-only Evidence Query API.
- Core never imports Gateway Python modules and never receives Gateway database
  credentials.
- The control-plane and witness authority is external to a cloneable Core data
  backup. The default backend is unavailable and therefore fail-closed.
- An in-memory 2-of-3 authority is available only for deterministic tests.

## Safe commands

Set `CORE_DATABASE_URL` to an explicitly named Core test database such as
`qingyu_core_test`. The safety gate checks both the configured URL and
`current_database()` before migrations or tests perform writes.

Both blocks below are run from the `core/` directory, inside that directory's
own virtualenv: `cd core`, then `python3.12 -m venv .venv`,
`source .venv/bin/activate` and `pip install -e ".[dev]"`. The Gateway commands
in `gateway/README.md` belong to `gateway/` and its venv instead.

### Linux / macOS (bash)

```bash
export CORE_DATABASE_URL="postgresql+psycopg://<core-owner>@127.0.0.1:5432/qingyu_core_test"
python -m alembic upgrade head
python -m pytest            # plain `pytest` works too: core/pyproject.toml puts core/ on sys.path
python -m compileall -q app tests
python -m pip check
python -m alembic check
```

### Windows (PowerShell)

```powershell
$env:CORE_DATABASE_URL = "postgresql+psycopg://<core-owner>@127.0.0.1:5432/qingyu_core_test"
python -m alembic upgrade head
python -m pytest
python -m compileall -q app tests
python -m pip check
python -m alembic check
```

No migration creates a Qingyu identity. GENESIS is an explicit, authorized
operation mediated by the non-cloned control plane.
