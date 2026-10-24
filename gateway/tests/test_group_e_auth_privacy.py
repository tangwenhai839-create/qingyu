import logging

import pytest
from app.config import Settings
from app.db import engine
from app.errors import safe_database_error, safe_log_exception
from app.main import app
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError


@pytest.mark.parametrize('token',['','changeme','example','change-this-local-token'])
def test_non_test_runtime_rejects_public_or_empty_tokens(token):
    with pytest.raises(ValueError,match='API_TOKEN'):
        Settings(app_env='production',api_token=token,_env_file=None)


def test_sqlalchemy_hides_secret_parameters():
    marker='SUPER_SECRET_CHAT_CONTENT_123'
    assert engine.hide_parameters is True
    with engine.connect() as connection, pytest.raises(Exception) as caught:
        connection.execute(text('select cast(:value as integer)'),{'value':marker})
    assert marker not in safe_database_error(caught.value)


def test_api_database_error_and_auth_logs_do_not_leak_secrets(caplog, capsys):
    marker='SUPER_SECRET_CHAT_CONTENT_123'; token='SUPER_SECRET_TOKEN_456'
    if not any(getattr(route,'path',None)=='/_test_database_error' for route in app.routes):
        @app.get('/_test_database_error')
        def fail():
            with engine.connect() as connection:
                connection.execute(text('select cast(:value as integer)'), {'value': marker})
    caplog.set_level(logging.INFO)
    response=TestClient(app).get('/_test_database_error')
    assert response.status_code==500
    assert response.json()['detail']['code']=='database_operation_failed'
    assert response.json()['detail']['request_id']
    TestClient(app).get('/api/v1/protected-ping',headers={'Authorization':f'Bearer {token}'})
    captured=capsys.readouterr()
    output=caplog.text+captured.out+captured.err+response.text
    assert marker not in output and token not in output


def test_safe_logger_never_formats_exception_message(caplog, capsys):
    marker='SUPER_SECRET_CHAT_CONTENT_123'
    caplog.set_level(logging.ERROR)
    safe_log_exception(logging.getLogger('privacy-test'),logging.ERROR,'safe failure',error_code='database_operation_failed',operation='test',exception=SQLAlchemyError(marker),job_id='safe-job')
    captured=capsys.readouterr()
    output=caplog.text+captured.out+captured.err
    assert marker not in output
    record=caplog.records[-1]
    assert record.error_type=='SQLAlchemyError' and record.error_code=='database_operation_failed'
