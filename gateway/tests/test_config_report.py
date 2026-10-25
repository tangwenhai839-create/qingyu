"""Startup configuration report: host and presence, never secret values."""

import pytest
from app.config import Settings, startup_config_report
from pydantic_settings import SettingsConfigDict

_API_TOKEN = "a-real-token"
_READ_TOKEN = "another-real-token"
_CURSOR_KEY = "a-third-secret"
_PASSWORD = "hunter2"
_USER = "dbuser"


class _IsolatedSettings(Settings):
    """Init kwargs only, so a developer or CI environment cannot fill fields."""

    model_config = SettingsConfigDict(extra="ignore")

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        return (init_settings,)


def _settings(*, app_env: str = "test", **overrides: object) -> Settings:
    """Build Settings with the field name ``app_env`` exactly once.

    Passing ``APP_ENV`` in ``overrides`` and again as a constructor argument
    raises ``TypeError: multiple values for keyword argument``.
    """
    values: dict[str, object] = {
        "app_env": app_env,
        "host": "127.0.0.1",
        "port": 8000,
        "log_level": "INFO",
        "archive_root": "/var/lib/qingyu",
        "api_token": _API_TOKEN,
        "api_tokens": "",
        "evidence_read_api_token": _READ_TOKEN,
        "evidence_read_api_tokens": "",
        "evidence_cursor_signing_key": _CURSOR_KEY,
        "metrics_token": "",
        "database_url": None,
        "_env_file": None,
    }
    values.update(overrides)
    return _IsolatedSettings(**values)


def test_report_lists_host_and_secret_presence_never_values():
    settings = _settings(
        app_env="development",
        database_url=f"postgresql+psycopg://{_USER}:{_PASSWORD}@db.example.com:5432/qingyu",
        metrics_token="metrics-secret-value",
    )
    report = startup_config_report(settings)
    assert "app_env=development" in report
    assert "listen=127.0.0.1:8000" in report
    assert "log_level=INFO" in report
    assert "archive_root=/var/lib/qingyu" in report
    assert "database=set (host=db.example.com)" in report
    assert "api_token=yes" in report
    assert "evidence_read_api_token=yes" in report
    assert "evidence_cursor_signing_key=yes" in report
    assert "metrics_token=yes" in report
    assert _PASSWORD not in report
    assert _USER not in report
    assert _API_TOKEN not in report
    assert _READ_TOKEN not in report
    assert _CURSOR_KEY not in report
    assert "metrics-secret-value" not in report
    assert "5432" not in report
    assert "\n" not in report


def test_report_marks_unset_secrets_and_database(monkeypatch):
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+psycopg://ambient:ambient-password@ambient.example:5432/qingyu",
    )
    monkeypatch.setenv("API_TOKEN", "ambient-token-should-not-appear")
    monkeypatch.setenv("APP_ENV", "production")
    settings = _settings(database_url=None, api_token="   ", metrics_token="")
    report = startup_config_report(settings)
    assert "ambient-password" not in report
    assert "ambient.example" not in report
    assert "ambient-token-should-not-appear" not in report
    assert "app_env=test" in report
    assert "database=NOT SET" in report
    assert "api_token=no" in report
    assert "api_tokens=no" in report
    assert "evidence_read_api_token=yes" in report
    assert "evidence_cursor_signing_key=yes" in report
    assert "metrics_token=no" in report
    assert _API_TOKEN not in report


def test_missing_database_url_outside_test_fails_with_a_pointer():
    with pytest.raises(RuntimeError, match=r"DATABASE_URL is not set") as raised:
        startup_config_report(_settings(app_env="development", database_url=None))
    message = str(raised.value)
    assert "gateway/.env.example" in message
    assert _API_TOKEN not in message
    assert _PASSWORD not in message
    with pytest.raises(RuntimeError, match=r"DATABASE_URL is not set"):
        startup_config_report(_settings(app_env="production", database_url="   "))


def test_unparsable_database_url_outside_test_does_not_echo_the_value():
    raw = f"not-a-valid-url-{_PASSWORD}"
    with pytest.raises(RuntimeError, match=r"database_url_invalid") as raised:
        startup_config_report(_settings(app_env="development", database_url=raw))
    message = str(raised.value)
    assert "gateway/.env.example" in message
    assert _PASSWORD not in message
    assert raw not in message


def test_test_env_needs_no_database_url():
    assert "database=NOT SET" in startup_config_report(_settings(database_url=None))
    assert "database=NOT SET" in startup_config_report(
        _settings(app_env="Test", database_url="  ")
    )
