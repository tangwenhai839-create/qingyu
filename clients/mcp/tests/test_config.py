"""Settings name variables and do not echo tokens."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from qingyu_mcp.config import (
    ENV_BASE_URL,
    ENV_INGEST_TOKEN,
    ENV_PENDING_PATH,
    ENV_READ_TOKEN,
    ConfigError,
    configure_logging,
    install_redaction,
    load_settings,
)

from tests.helpers import INGEST, READ, make_settings


def _env(**overrides: str) -> dict[str, str]:
    values = {
        ENV_BASE_URL: "http://127.0.0.1:8000/",
        ENV_READ_TOKEN: READ,
        ENV_INGEST_TOKEN: INGEST,
    }
    values.update(overrides)
    return values


def test_load_settings_strips_the_origin_and_hides_tokens(tmp_path: Path) -> None:
    loaded = load_settings(
        _env(**{ENV_PENDING_PATH: str(tmp_path / "rel" / "pending.jsonl")})
    )
    assert loaded.base_url == "http://127.0.0.1:8000"
    assert loaded.pending_path.is_absolute()
    text = repr(loaded)
    assert READ not in text
    assert INGEST not in text
    assert "[redacted]" in text


def test_missing_variable_does_not_echo_a_provided_token() -> None:
    environ = {ENV_READ_TOKEN: READ, ENV_INGEST_TOKEN: INGEST}
    with pytest.raises(ConfigError) as caught:
        load_settings(environ)
    message = str(caught.value)
    assert READ not in message
    assert INGEST not in message
    assert ENV_BASE_URL in message


def test_userinfo_and_shared_tokens_are_rejected_without_echoing_them() -> None:
    secret = "userinfo-secret-value"
    with pytest.raises(ConfigError) as userinfo:
        load_settings(_env(**{ENV_BASE_URL: f"http://reader:{secret}@127.0.0.1:8000"}))
    assert secret not in str(userinfo.value)
    assert "userinfo" in str(userinfo.value)

    with pytest.raises(ConfigError) as shared:
        load_settings(_env(**{ENV_READ_TOKEN: READ, ENV_INGEST_TOKEN: READ}))
    assert READ not in str(shared.value)
    assert "must differ" in str(shared.value)

    short = "too-short"
    with pytest.raises(ConfigError) as tiny:
        load_settings(_env(**{ENV_READ_TOKEN: short}))
    assert short not in str(tiny.value)
    assert ENV_READ_TOKEN in str(tiny.value)


def test_startup_log_and_header_objects_do_not_contain_tokens(
    tmp_path: Path, capfd: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    settings = make_settings(tmp_path)
    configure_logging(settings)
    captured = capfd.readouterr()
    assert "stdio adapter ready" in captured.err
    assert READ not in captured.err
    assert INGEST not in captured.err

    install_redaction(settings.secret_values())
    logging.getLogger("qingyu_mcp").propagate = True
    logging.getLogger("httpx").setLevel(logging.DEBUG)
    caplog.set_level(logging.DEBUG)

    class Headers:
        def __str__(self) -> str:
            return f"Authorization: Bearer {READ}"

    logging.getLogger("httpx._client").debug("headers %s", Headers())
    try:
        raise RuntimeError(f"Bearer {INGEST}")
    except RuntimeError:
        logging.getLogger("qingyu_mcp").exception("request failed")

    assert READ not in caplog.text
    assert INGEST not in caplog.text
    assert "[redacted]" in caplog.text
