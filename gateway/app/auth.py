import hashlib
import secrets
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import Settings, get_settings

bearer = HTTPBearer(auto_error=False)


def token_matches(supplied: str, candidates: tuple[str, ...]) -> bool:
    """Return whether ``supplied`` matches any candidate.

    Every candidate is compared. A match does not skip the remaining
    comparisons, and each comparison uses ``secrets.compare_digest``.
    """
    matched = False
    for candidate in candidates:
        matched = secrets.compare_digest(supplied, candidate) or matched
    return matched


def require_token(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)], settings: Annotated[Settings, Depends(get_settings)]) -> None:
    if credentials is None or credentials.scheme.lower() != "bearer" or not token_matches(credentials.credentials, settings.active_api_tokens()):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing bearer token")


def require_evidence_read_token(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    """Authorize the read-only Evidence API without granting mutation access.

    The operator API tokens and every configured evidence-read token are
    active at the same time so either credential can be rotated by overlap.
    """
    if not credentials or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "authentication_required"},
        )
    supplied = credentials.credentials
    api_authorized = token_matches(supplied, settings.active_api_tokens())
    read_authorized = token_matches(supplied, settings.active_evidence_read_tokens())
    if not (api_authorized or read_authorized):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "authentication_required"},
        )


def require_ingest_client(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> str:
    """Return the client_id for an ingest bearer token.

    The operator token and the evidence-read token are not ingest credentials.
    Comparison is against SHA-256 hashes from INGEST_CLIENTS, not stored secrets.
    """
    if not credentials or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "authentication_required"},
        )
    supplied = credentials.credentials
    if token_matches(supplied, settings.active_api_tokens()) or token_matches(
        supplied, settings.active_evidence_read_tokens()
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "authentication_required"},
        )
    client_id = match_ingest_client(supplied, settings.ingest_client_directory())
    if client_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "authentication_required"},
        )
    return client_id


def match_ingest_client(supplied: str, directory: dict[str, tuple[str, ...]]) -> str | None:
    """Return the one client whose stored hash matches ``supplied``.

    Every configured hash is compared. A hash that maps to two clients matches
    nothing.
    """
    digest = hashlib.sha256(supplied.encode("utf-8")).hexdigest()
    matched: list[str] = []
    for client_id, hashes in directory.items():
        hit = False
        for candidate in hashes:
            hit = secrets.compare_digest(digest, candidate) or hit
        if hit:
            matched.append(client_id)
    if len(matched) != 1:
        return None
    return matched[0]
