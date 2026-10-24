from typing import Annotated
from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
import logging
import uuid
from .errors import safe_database_error, safe_log_exception
from .auth import require_token
from .config import get_settings
from .logging_config import configure_logging
from .request_limits import (
    FixedWindowRateLimiter,
    ImportRequestSizeLimitMiddleware,
    RateLimitMiddleware,
)
from .api_import import router as import_router
from .api_archive import router as archive_router
from .api_evidence import router as evidence_router
from .api_export import archive_router as export_archive_router
from .api_export import conversation_router as export_conversation_router

settings = get_settings()
configure_logging(settings.log_level)
app = FastAPI(title=settings.app_name, version="0.1.0")
rate_limiter = FixedWindowRateLimiter(
    limit=settings.rate_limit_requests,
    window_seconds=settings.rate_limit_window_seconds,
)
app.state.rate_limiter = rate_limiter
app.add_middleware(ImportRequestSizeLimitMiddleware)
app.add_middleware(RateLimitMiddleware, limiter=rate_limiter)
app.include_router(archive_router)
app.include_router(import_router)
app.include_router(evidence_router)
app.include_router(export_conversation_router)
app.include_router(export_archive_router)


@app.middleware("http")
async def evidence_response_cache_policy(request, call_next):
    is_evidence = request.url.path.startswith("/api/v1/evidence")
    try:
        response = await call_next(request)
    except Exception as exc:
        if not is_evidence:
            raise
        request_id = uuid.uuid4().hex
        safe_log_exception(
            logging.getLogger(__name__),
            logging.ERROR,
            "evidence request failed",
            error_code="evidence_operation_failed",
            operation="evidence_query",
            exception=exc,
            correlation_id=request_id,
        )
        response = JSONResponse(
            status_code=500,
            content={
                "detail": {
                    "code": "evidence_operation_failed",
                    "request_id": request_id,
                }
            },
        )
    if is_evidence:
        response.headers["Cache-Control"] = "private, no-store"
    return response

@app.exception_handler(SQLAlchemyError)
async def database_error_handler(request, exc):
    error_code = safe_database_error(exc)
    request_id = uuid.uuid4().hex
    safe_log_exception(
        logging.getLogger(__name__),
        logging.ERROR,
        "database operation failed",
        error_code=error_code,
        operation="api_request",
        exception=exc,
        correlation_id=request_id,
    )
    return JSONResponse(status_code=500, content={"detail": {"code": error_code, "request_id": request_id}})

@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": settings.app_name, "version": "0.1.0"}

@app.get("/api/v1/protected-ping")
def protected_ping(_: Annotated[None, Depends(require_token)]) -> dict[str, str]:
    return {"status": "ok"}
