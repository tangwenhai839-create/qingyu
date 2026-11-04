"""Process configuration. Tokens stay in the environment and off the logs."""

from __future__ import annotations

import logging
import os
import sys
import traceback
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

ENV_BASE_URL = "QINGYU_GATEWAY_BASE_URL"
ENV_READ_TOKEN = "QINGYU_EVIDENCE_READ_TOKEN"
ENV_INGEST_TOKEN = "QINGYU_INGEST_TOKEN"
ENV_PENDING_PATH = "QINGYU_MCP_PENDING_PATH"

_REQUIRED = (ENV_BASE_URL, ENV_READ_TOKEN, ENV_INGEST_TOKEN)
_MIN_TOKEN_LENGTH = 16
_REDACTED = "[redacted]"
_secrets: list[str] = []
_patched = False
_original_handle = logging.Logger.handle


class ConfigError(Exception):
    """Startup configuration is incomplete. The message names variables only."""


@dataclass(frozen=True)
class Settings:
    base_url: str
    read_token: str
    ingest_token: str
    pending_path: Path
    timeout_seconds: float = 30.0

    def __repr__(self) -> str:
        return (
            "Settings("
            f"base_url={self.base_url!r}, "
            f"pending_path={str(self.pending_path)!r}, "
            "read_token='[redacted]', ingest_token='[redacted]')"
        )

    def secret_values(self) -> tuple[str, str]:
        return (self.read_token, self.ingest_token)


def default_pending_path() -> Path:
    return Path.home() / ".local" / "state" / "qingyu-mcp" / "pending.jsonl"


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if environ is None else environ
    missing = [name for name in _REQUIRED if not env.get(name, "").strip()]
    if missing:
        raise ConfigError("missing environment: " + ", ".join(missing))

    read_token = env[ENV_READ_TOKEN].strip()
    ingest_token = env[ENV_INGEST_TOKEN].strip()
    short = [
        name
        for name, value in (
            (ENV_READ_TOKEN, read_token),
            (ENV_INGEST_TOKEN, ingest_token),
        )
        if len(value) < _MIN_TOKEN_LENGTH
    ]
    if short:
        raise ConfigError("token too short: " + ", ".join(short))
    if read_token == ingest_token:
        raise ConfigError(f"{ENV_READ_TOKEN} and {ENV_INGEST_TOKEN} must differ")

    raw_url = env[ENV_BASE_URL].strip()
    parsed = urlsplit(raw_url)
    if parsed.username is not None or parsed.password is not None:
        raise ConfigError(f"{ENV_BASE_URL} must not include userinfo")
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ConfigError(f"{ENV_BASE_URL} must be an http(s) origin")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ConfigError(f"{ENV_BASE_URL} must be an origin without a path or query")
    base_url = f"{parsed.scheme}://{parsed.netloc}"

    raw_pending = env.get(ENV_PENDING_PATH, "").strip()
    pending = Path(raw_pending).expanduser() if raw_pending else default_pending_path()
    pending = pending.resolve(strict=False)
    if pending.is_dir():
        raise ConfigError(f"{ENV_PENDING_PATH} must be a file path")
    return Settings(
        base_url=base_url,
        read_token=read_token,
        ingest_token=ingest_token,
        pending_path=pending,
    )


def redact(text: str, secrets: Sequence[str] | None = None) -> str:
    """Replace configured token values. Short strings are left alone."""
    redacted = text
    values = secrets if secrets is not None else _secrets
    for secret in sorted((item for item in values if len(item) >= 8), key=len, reverse=True):
        if secret in redacted:
            redacted = redacted.replace(secret, _REDACTED)
    return redacted


def install_redaction(secrets: Sequence[str]) -> None:
    """Redact tokens in every log line, including headers stringified by httpx."""
    global _patched
    _secrets[:] = [secret for secret in secrets if len(secret) >= 8]
    if _patched:
        return

    def handle(self: logging.Logger, record: logging.LogRecord) -> None:
        _redact_record(record)
        _original_handle(self, record)

    logging.Logger.handle = handle  # type: ignore[method-assign]
    _patched = True


def _redact_record(record: logging.LogRecord) -> None:
    original = record.getMessage

    def get_message() -> str:
        return redact(original())

    record.getMessage = get_message  # type: ignore[method-assign]
    if not record.exc_info or record.exc_info[0] is None:
        return
    text = "".join(traceback.format_exception(*record.exc_info))
    cleaned = redact(text)
    if cleaned != text:
        record.exc_text = cleaned
        record.exc_info = None


def configure_logging(settings: Settings) -> None:
    """Send adapter logs to stderr. Stdout is reserved for MCP frames."""
    install_redaction(settings.secret_values())
    logger = logging.getLogger("qingyu_mcp")
    logger.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    logger.info(
        "stdio adapter ready base_url=%s pending=%s",
        settings.base_url,
        settings.pending_path,
    )
