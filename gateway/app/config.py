from functools import lru_cache
import secrets
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, field_validator, model_validator

_FORBIDDEN_SECRETS = {"", "changeme", "example", "change-this-local-token", "change-me"}


def normalize_log_format(value: str) -> str:
    normalized = (value or "").strip().lower()
    if normalized not in {"json", "text"}:
        raise ValueError("LOG_FORMAT must be json or text")
    return normalized


def split_token_list(value: str) -> tuple[str, ...]:
    """Split a comma-separated token list.

    Empty or whitespace-only values mean no additional tokens. A comma with a
    blank entry is rejected so a trailing comma cannot silently drop a token.
    Tokens must not contain commas; ``secrets.token_urlsafe`` values do not.
    """
    if not value or not value.strip():
        return ()
    parts = tuple(part.strip() for part in value.split(","))
    if any(part == "" for part in parts):
        raise ValueError("token list contains an empty entry")
    return parts


def combine_tokens(primary: str, additional: str) -> tuple[str, ...]:
    tokens: list[str] = []
    if primary:
        tokens.append(primary)
    tokens.extend(split_token_list(additional))
    return tuple(tokens)


def _reject_duplicates(tokens: tuple[str, ...], label: str) -> None:
    for index, token in enumerate(tokens):
        for other in tokens[index + 1 :]:
            if secrets.compare_digest(token, other):
                raise ValueError(f"{label} contains a duplicate token")


class Settings(BaseSettings):
    app_name: str = "Qingyu Memory Gateway"
    app_env: str = "development"
    host: str = "127.0.0.1"
    port: int = 8000
    api_token: str = ""
    api_tokens: str = ""
    evidence_read_api_token: str = ""
    evidence_read_api_tokens: str = ""
    evidence_cursor_signing_key: str = ""
    database_url: str | None = None
    log_level: str = "INFO"
    log_format: str = "text"
    metrics_enabled: bool = False
    metrics_token: str = ""
    archive_root: str = "<repo>/data"
    staging_failed_retention_days: int = Field(default=7, ge=0)
    staging_cleanup_interval_seconds: float = Field(default=300.0, gt=0)
    staging_cleanup_max_candidates: int = Field(default=100, gt=0)
    import_upload_max_bytes: int = Field(default=512 * 1024 * 1024, gt=0)
    request_body_max_bytes: int = Field(default=1024 * 1024, gt=0)
    rate_limit_enabled: bool = True
    rate_limit_requests: int = Field(default=600, gt=0)
    rate_limit_window_seconds: float = Field(default=60.0, gt=0)
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @field_validator("log_format")
    @classmethod
    def _valid_log_format(cls, value: str) -> str:
        return normalize_log_format(value)

    def active_api_tokens(self) -> tuple[str, ...]:
        """Primary API token plus every additional token. All are active."""
        return combine_tokens(self.api_token, self.api_tokens)

    def active_evidence_read_tokens(self) -> tuple[str, ...]:
        """Primary evidence-read token plus every additional read token."""
        return combine_tokens(self.evidence_read_api_token, self.evidence_read_api_tokens)

    @model_validator(mode="after")
    def secure_api_token(self):
        if self.app_env.lower() == "test":
            return self
        normalized = self.api_token.strip().lower()
        normalized_evidence = self.evidence_read_api_token.strip().lower()
        normalized_cursor_key = self.evidence_cursor_signing_key.strip().lower()
        if normalized in _FORBIDDEN_SECRETS:
            raise ValueError("API_TOKEN must be a non-example secret")
        if normalized_evidence in _FORBIDDEN_SECRETS:
            raise ValueError("EVIDENCE_READ_API_TOKEN must be a non-example secret")
        if normalized_cursor_key in _FORBIDDEN_SECRETS:
            raise ValueError("EVIDENCE_CURSOR_SIGNING_KEY must be a non-example secret")
        if secrets.compare_digest(self.api_token, self.evidence_read_api_token):
            raise ValueError("EVIDENCE_READ_API_TOKEN must differ from API_TOKEN")
        if secrets.compare_digest(self.evidence_cursor_signing_key, self.api_token):
            raise ValueError("EVIDENCE_CURSOR_SIGNING_KEY must differ from API_TOKEN")
        if secrets.compare_digest(
            self.evidence_cursor_signing_key, self.evidence_read_api_token
        ):
            raise ValueError(
                "EVIDENCE_CURSOR_SIGNING_KEY must differ from EVIDENCE_READ_API_TOKEN"
            )
        try:
            api_tokens = self.active_api_tokens()
            evidence_tokens = self.active_evidence_read_tokens()
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        for token in api_tokens:
            if token.strip().lower() in _FORBIDDEN_SECRETS:
                raise ValueError("API_TOKENS must be a non-example secret")
        for token in evidence_tokens:
            if token.strip().lower() in _FORBIDDEN_SECRETS:
                raise ValueError("EVIDENCE_READ_API_TOKENS must be a non-example secret")
        _reject_duplicates(api_tokens, "API_TOKENS")
        _reject_duplicates(evidence_tokens, "EVIDENCE_READ_API_TOKENS")
        for api_token in api_tokens:
            for evidence_token in evidence_tokens:
                if secrets.compare_digest(api_token, evidence_token):
                    raise ValueError("evidence-read tokens must differ from API tokens")
        for token in (*api_tokens, *evidence_tokens):
            if secrets.compare_digest(self.evidence_cursor_signing_key, token):
                raise ValueError(
                    "EVIDENCE_CURSOR_SIGNING_KEY must differ from every access token"
                )
        return self

@lru_cache
def get_settings() -> Settings:
    return Settings()
