# Qingyu Contracts

This repository is the versioned, lightweight contract boundary between the
frozen Gateway and Qingyu Core. It contains explicit Pydantic DTOs and checked
JSON Schema artifacts. It does not import Gateway ORM code, connect to either
PostgreSQL database, or implement Core persistence.

The first contract set is deliberately narrow:

- `evidence-query-v1`: the read-only Gateway Evidence Query API from
  `GATEWAY_FROZEN_MAINTENANCE_AMENDMENT_1.md`. `POST /api/v1/evidence/search`
  is `evidence-search-v1` (Amendment #2) and is not part of this package.
- `core-identity-v1`: opaque Core identity/continuity fields shared by future
  Core APIs.

Runtime Event, User Model, Relationship Model, Memory Intelligence, Journal,
and projection schemas are intentionally absent from this commit.

## Source and compatibility baseline

The Evidence DTOs were checked against the frozen Gateway implementation:

- `<repo>\gateway\app\evidence\schemas.py`
- `<repo>\gateway\app\api_evidence.py`
- `<repo>\gateway\app\auth.py`
- `<repo>\gateway\docs\GATEWAY_FROZEN_MAINTENANCE_AMENDMENT_1.md`

Gateway compatibility baseline:

- Amendment #1 frozen contract: `evidence-query-v1`
- Gateway migration head: `0018_ingest_requests`
- Gateway Phase 3 freeze baseline: `bb107edadce0168b14189b47e902fd4c31fe3f19`

The contract package is a consumer-side DTO boundary. A Core implementation
must use the Gateway HTTP API and must never import Gateway SQLAlchemy models or
use a Gateway database credential.

## Checks

From this repository:

```powershell
python -m pip install -e ".[dev]"
python scripts/generate_schemas.py
python -m pytest
```

`generate_schemas.py` writes only the two checked-in schema artifacts. It does
not read secrets or contact a service.
