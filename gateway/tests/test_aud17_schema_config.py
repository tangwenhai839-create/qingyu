import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from alembic.config import Config
from app.database_config import apply_alembic_database_url, configured_database_url
from app.db import engine
from app.models import ImportJob
from conftest import validated_url
from sqlalchemy import Integer, create_engine, inspect, text
from sqlalchemy.engine import make_url

COUNTER_DEFAULTS = {
    'discovered_count':'0', 'imported_count':'0', 'skipped_count':'0',
    'failed_count':'0', 'attempt_count':'0', 'max_attempts':'3',
}


def _column_map(target_engine, table):
    return {column['name']:column for column in inspect(target_engine).get_columns(table)}


def _assert_import_job_contract(target_engine):
    actual=_column_map(target_engine,'import_jobs')
    model={column.name:column for column in ImportJob.__table__.columns}
    for name, default in COUNTER_DEFAULTS.items():
        assert model[name].nullable is False
        assert actual[name]['nullable'] is False
        assert isinstance(actual[name]['type'],Integer)
        assert default in str(actual[name]['default'])
    inspector=inspect(target_engine)
    source_uniques={item['name'] for item in inspector.get_unique_constraints('source_accounts')}
    message_uniques={item['name'] for item in inspector.get_unique_constraints('messages')}
    revision_uniques={item['name'] for item in inspector.get_unique_constraints('message_revisions')}
    observation_uniques={item['name'] for item in inspector.get_unique_constraints('message_observations')}
    assert 'uq_source_accounts_platform_label' in source_uniques
    assert 'uq_messages_conversation_external' in message_uniques
    assert {'uq_message_revisions_no','uq_message_revisions_fingerprint'} <= revision_uniques
    assert 'uq_message_observations_job_revision_node' in observation_uniques
    foreign_keys={tuple(item['constrained_columns']):tuple(item['referred_columns']) for item in inspector.get_foreign_keys('import_jobs')}
    assert foreign_keys[('source_account_id',)]==('id',)
    assert foreign_keys[('archive_object_id',)]==('id',)
    indexes={item['name']:tuple(item['column_names']) for item in inspector.get_indexes('import_jobs')}
    assert indexes['ix_import_jobs_lease_token']==('lease_token',)


def _run_alembic(url, *arguments):
    env=os.environ.copy(); env.update({'DATABASE_URL':url,'APP_ENV':'test','API_TOKEN':'migration-test-token'})
    return subprocess.run([sys.executable,'-m','alembic',*arguments],cwd=Path(__file__).parents[1],env=env,capture_output=True,text=True,timeout=30,check=True)


def _environment_without_database_url():
    env=os.environ.copy()
    env.pop('DATABASE_URL',None)
    env.update({'APP_ENV':'test','API_TOKEN':'SUPER_SECRET_TOKEN_456'})
    return env


@pytest.fixture()
def disposable_postgres_database():
    name=f"qingyu_disposable_{uuid.uuid4().hex[:12]}"
    admin_url=validated_url.set(database='postgres')
    admin=create_engine(admin_url,isolation_level='AUTOCOMMIT')
    with admin.connect() as connection:
        connection.exec_driver_sql(f'CREATE DATABASE "{name}"')
    database_url=validated_url.set(database=name)
    try:
        yield database_url.render_as_string(hide_password=False)
    finally:
        with admin.connect() as connection:
            connection.execute(text('SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=:name AND pid<>pg_backend_pid()'),{'name':name})
            connection.exec_driver_sql(f'DROP DATABASE IF EXISTS "{name}"')
        admin.dispose()


def test_current_test_database_matches_model_schema_contract():
    with engine.connect() as connection:
        assert connection.scalar(text('select current_database()'))=='qingyu_test'
    _assert_import_job_contract(engine)


def test_runtime_and_alembic_resolve_the_same_settings_url():
    config=Config('alembic.ini'); config.set_main_option('sqlalchemy.url','postgresql+psycopg://stale:PASSWORD@127.0.0.1/db_b')
    resolved=apply_alembic_database_url(config)
    assert make_url(resolved)==make_url(config.get_main_option('sqlalchemy.url'))
    assert make_url(resolved)==engine.url==make_url(configured_database_url())
    assert engine.url.database=='qingyu_test'


def test_runtime_fails_closed_without_database_url_or_env_file(tmp_path):
    result=subprocess.run(
        [sys.executable,'-m','app.importers.worker_runtime'],
        cwd=tmp_path,
        env={**_environment_without_database_url(),'PYTHONPATH':str(Path(__file__).parents[1])},
        capture_output=True,text=True,timeout=15,check=False,
    )
    output=result.stdout+result.stderr
    assert result.returncode != 0
    assert 'database_url_required' in output
    assert 'Traceback' not in output
    assert 'SUPER_SECRET_TOKEN_456' not in output
    assert 'postgresql' not in output.lower()
    assert '/qingyu' not in output.lower()


def test_alembic_fails_closed_without_database_url_and_ignores_stale_ini(tmp_path):
    repository=Path(__file__).parents[1]
    stale='postgresql+psycopg://stale:PASSWORD@127.0.0.1/db_b'
    config=tmp_path/'alembic.ini'
    config.write_text(
        '[alembic]\n'
        f'script_location = {repository / "alembic"}\n'
        f'prepend_sys_path = {repository}\n'
        f'sqlalchemy.url = {stale}\n',
        encoding='utf-8',
    )
    result=subprocess.run(
        [sys.executable,'-m','alembic','-c',str(config),'current'],
        cwd=tmp_path,env=_environment_without_database_url(),capture_output=True,text=True,timeout=15,check=False,
    )
    output=result.stdout+result.stderr
    assert result.returncode != 0
    assert 'database_url_required' in output
    assert 'Traceback' not in output
    assert 'SUPER_SECRET_PASSWORD' not in output
    assert 'SUPER_SECRET_TOKEN_456' not in output
    assert stale not in output


@pytest.mark.parametrize('command',[
    [sys.executable,'-m','app.importers.worker_runtime'],
    [sys.executable,'-m','alembic','-c','alembic.ini','current'],
])
def test_invalid_database_url_is_rejected_without_secret_or_traceback(tmp_path,command):
    repository=Path(__file__).parents[1]
    if 'alembic' in command:
        config=tmp_path/'alembic.ini'
        config.write_text(
            '[alembic]\n'
            f'script_location = {repository / "alembic"}\n'
            f'prepend_sys_path = {repository}\n'
            'sqlalchemy.url = postgresql+psycopg://unused/unused\n',
            encoding='utf-8',
        )
    env=_environment_without_database_url()
    env.update({'DATABASE_URL':'not-a-valid-url-SUPER_SECRET_DB_PASSWORD_123','PYTHONPATH':str(repository)})
    result=subprocess.run(command,cwd=tmp_path,env=env,capture_output=True,text=True,timeout=15,check=False)
    output=result.stdout+result.stderr
    assert result.returncode != 0
    assert 'database_url_invalid' in output
    assert 'Traceback' not in output
    assert 'SUPER_SECRET_DB_PASSWORD_123' not in output
    assert 'SUPER_SECRET_TOKEN_456' not in output


def test_alembic_connection_failure_is_redacted_without_traceback():
    env=os.environ.copy()
    env.update({
        'DATABASE_URL':'postgresql+psycopg://audit:PASSWORD@127.0.0.1:1/unreachable?connect_timeout=2',
        'APP_ENV':'test',
        'API_TOKEN':'SUPER_SECRET_TOKEN_456',
    })
    result=subprocess.run(
        [sys.executable,'-m','alembic','current'],
        cwd=Path(__file__).parents[1],env=env,capture_output=True,text=True,timeout=15,check=False,
    )
    output=result.stdout+result.stderr
    assert result.returncode != 0
    assert 'database_operation_failed' in output
    assert 'Traceback' not in output
    assert 'SUPER_SECRET_DB_PASSWORD_123' not in output
    assert 'SUPER_SECRET_TOKEN_456' not in output


def test_fresh_postgresql_upgrade_schema_and_deterministic_downgrade(disposable_postgres_database):
    _run_alembic(disposable_postgres_database,'upgrade','head')
    fresh=create_engine(disposable_postgres_database)
    try:
        with fresh.connect() as connection:
            assert connection.scalar(text('select version_num from alembic_version'))=='0016_real_export_evidence'
        _assert_import_job_contract(fresh)
        _run_alembic(disposable_postgres_database,'downgrade','0012_import_identity_constraints')
        downgraded=_column_map(fresh,'import_jobs')
        assert all(downgraded[name]['nullable'] is True for name in COUNTER_DEFAULTS)
        assert downgraded['discovered_count']['default'] is None
        assert '0' in str(downgraded['attempt_count']['default'])
        assert '3' in str(downgraded['max_attempts']['default'])
        _run_alembic(disposable_postgres_database,'upgrade','head')
        fresh.dispose(); fresh=create_engine(disposable_postgres_database)
        _assert_import_job_contract(fresh)
    finally:
        fresh.dispose()
