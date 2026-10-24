import os
import re

import pytest
from conftest import (
    get_expected_alembic_head,
    validate_test_database_url,
    verify_connected_database,
    verify_database_migrations,
)
from sqlalchemy import create_engine, text
from sqlalchemy.exc import ProgrammingError


class _Connection:
    def __init__(self, database): self.database = database
    def __enter__(self): return self
    def __exit__(self, *args): return None
    def scalar(self, statement): return self.database


class _Engine:
    def __init__(self, database): self.database = database
    def connect(self): return _Connection(self.database)


def test_rejects_persistent_database_url():
    with pytest.raises(RuntimeError):
        validate_test_database_url("postgresql+psycopg://u:PASSWORD@localhost/qingyu")


def test_allows_exact_test_database_and_runtime_target():
    url = validate_test_database_url("postgresql+psycopg://u:PASSWORD@localhost/qingyu_test")
    assert url.database == "qingyu_test"
    assert verify_connected_database(_Engine("qingyu_test")) == "qingyu_test"


def test_rejects_query_database_override():
    with pytest.raises(RuntimeError, match="forbidden database override"):
        validate_test_database_url("postgresql+psycopg://u:PASSWORD@localhost/qingyu_test?dbname=qingyu")


def test_rejects_runtime_database_mismatch():
    with pytest.raises(RuntimeError, match="connected database 'qingyu'"):
        verify_connected_database(_Engine("qingyu"))


def test_rejects_similarly_named_database():
    with pytest.raises(RuntimeError):
        validate_test_database_url("postgresql+psycopg://u:PASSWORD@localhost/my_test_backup")


def _declared_revisions() -> set[str]:
    versions_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "alembic", "versions"
    )
    revisions = set()
    for name in os.listdir(versions_dir):
        if not name.endswith(".py"):
            continue
        with open(os.path.join(versions_dir, name), encoding="utf-8") as handle:
            source = handle.read()
        match = re.search(r"^revision\s*(?::[^=]+)?=\s*['\"]([^'\"]+)['\"]", source, re.MULTILINE)
        if match:
            revisions.add(match.group(1))
    return revisions


def test_get_expected_alembic_head():
    # Deliberately not pinned to a revision id: it has to stay green when a new migration lands.
    head = get_expected_alembic_head()
    assert isinstance(head, str) and head
    assert head in _declared_revisions()


def test_verify_database_migrations_success():
    class _MigrationConnection:
        def __enter__(self): return self
        def __exit__(self, *args): return None
        def scalar(self, statement): return "0016_real_export_evidence"

    class _MigrationEngine:
        def connect(self): return _MigrationConnection()

    assert (
        verify_database_migrations(_MigrationEngine(), expected_head="0016_real_export_evidence")
        == "0016_real_export_evidence"
    )


def test_verify_database_migrations_mismatched_revision():
    class _MigrationConnection:
        def __enter__(self): return self
        def __exit__(self, *args): return None
        def scalar(self, statement): return "0001_phase1_baseline"

    class _MigrationEngine:
        def connect(self): return _MigrationConnection()

    with pytest.raises(
        RuntimeError, match=r"run: DATABASE_URL=\$TEST_DATABASE_URL APP_ENV=test alembic upgrade head"
    ):
        verify_database_migrations(_MigrationEngine(), expected_head="0016_real_export_evidence")


def test_verify_database_migrations_missing_table():
    class _MigrationConnection:
        def __enter__(self): return self
        def __exit__(self, *args): return None
        def scalar(self, statement): return None

    class _MigrationEngine:
        def connect(self): return _MigrationConnection()

    with pytest.raises(
        RuntimeError, match=r"run: DATABASE_URL=\$TEST_DATABASE_URL APP_ENV=test alembic upgrade head"
    ):
        verify_database_migrations(_MigrationEngine(), expected_head="0016_real_export_evidence")


def test_verify_database_migrations_unreadable_table():
    # The fixture only catches RuntimeError, so a database error from the SELECT has to be
    # converted here — otherwise the raw ProgrammingError replaces the actionable message.
    class _MigrationConnection:
        def __enter__(self): return self
        def __exit__(self, *args): return None
        def scalar(self, statement):
            raise ProgrammingError(
                "SELECT version_num FROM alembic_version",
                {},
                Exception('relation "alembic_version" does not exist'),
            )

    class _MigrationEngine:
        def connect(self): return _MigrationConnection()

    with pytest.raises(
        RuntimeError, match=r"run: DATABASE_URL=\$TEST_DATABASE_URL APP_ENV=test alembic upgrade head"
    ):
        verify_database_migrations(_MigrationEngine(), expected_head="0016_real_export_evidence")


def test_verify_database_migrations_with_sqlite():
    engine = create_engine("sqlite:///:memory:")
    with pytest.raises(
        RuntimeError, match=r"run: DATABASE_URL=\$TEST_DATABASE_URL APP_ENV=test alembic upgrade head"
    ):
        verify_database_migrations(engine, expected_head="0016_real_export_evidence")

    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
        conn.execute(text("INSERT INTO alembic_version VALUES ('0016_real_export_evidence')"))

    assert (
        verify_database_migrations(engine, expected_head="0016_real_export_evidence")
        == "0016_real_export_evidence"
    )
