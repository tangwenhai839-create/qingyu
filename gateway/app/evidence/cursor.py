import base64
import hashlib
import hmac
import json
from typing import Any

CURSOR_VERSION = 1


class InvalidCursor(ValueError):
    pass


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except Exception as exc:
        raise InvalidCursor("invalid_base64") from exc


def encode_cursor(payload: dict[str, Any], secret: str) -> str:
    document = {"v": CURSOR_VERSION, **payload}
    raw = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).digest()
    return f"{_b64encode(raw)}.{_b64encode(signature)}"


def decode_cursor(value: str, secret: str) -> dict[str, Any]:
    try:
        encoded_payload, encoded_signature = value.split(".", 1)
    except ValueError as exc:
        raise InvalidCursor("invalid_shape") from exc
    raw = _b64decode(encoded_payload)
    signature = _b64decode(encoded_signature)
    expected = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).digest()
    if not hmac.compare_digest(signature, expected):
        raise InvalidCursor("invalid_signature")
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidCursor("invalid_payload") from exc
    if not isinstance(payload, dict) or payload.get("v") != CURSOR_VERSION:
        raise InvalidCursor("unsupported_version")
    return payload
