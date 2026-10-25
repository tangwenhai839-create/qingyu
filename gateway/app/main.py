import logging
import uuid
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy.exc import SQLAlchemyError

from .auth import require_token
from .config import get_settings, startup_config_report
from .errors import safe_database_error, safe_log_exception
from .logging_config import configure_logging
from .observability import (
    MetricsRegistry,
    ObservabilityMiddleware,
    authorize_metrics,
    current_request_id,
)
from .request_limits import (
    FixedWindowRateLimiter,
    ImportRequestSizeLimitMiddleware,
    RateLimitMiddleware,
)

settings = get_settings()
configure_logging(settings.log_level, settings.log_format)
# Routers import the database engine. Report first so a missing DATABASE_URL
# fails here, with a pointer at gateway/.env.example, instead of inside that import.
logging.getLogger(__name__).info("%s", startup_config_report(settings))
app = FastAPI(title=settings.app_name, version="0.1.0")
rate_limiter = FixedWindowRateLimiter(
    limit=settings.rate_limit_requests,
    window_seconds=settings.rate_limit_window_seconds,
)
app.state.rate_limiter = rate_limiter
app.state.metrics = MetricsRegistry()
app.add_middleware(ImportRequestSizeLimitMiddleware)
app.add_middleware(RateLimitMiddleware, limiter=rate_limiter)


def _include_routers(application: FastAPI) -> None:
    from .api_archive import router as archive_router
    from .api_evidence import router as evidence_router
    from .api_export import archive_router as export_archive_router
    from .api_export import conversation_router as export_conversation_router
    from .api_import import router as import_router

    application.include_router(archive_router)
    application.include_router(import_router)
    application.include_router(evidence_router)
    application.include_router(export_conversation_router)
    application.include_router(export_archive_router)


_include_routers(app)


@app.middleware("http")
async def evidence_response_cache_policy(request, call_next):
    is_evidence = request.url.path.startswith("/api/v1/evidence")
    try:
        response = await call_next(request)
    except Exception as exc:
        if not is_evidence:
            raise
        request_id = current_request_id() or uuid.uuid4().hex
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

# Outermost: request ids and access logs cover rate-limit and size-limit
# responses, and the evidence middleware still sees the request id if it
# builds an error response.
app.add_middleware(ObservabilityMiddleware, metrics=app.state.metrics)

@app.exception_handler(SQLAlchemyError)
async def database_error_handler(request, exc):
    error_code = safe_database_error(exc)
    request_id = current_request_id() or uuid.uuid4().hex
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


@app.get("/health/ready")
def health_ready() -> JSONResponse:
    """Readiness probe. 200 after SELECT 1, 503 when the database does not answer."""
    from sqlalchemy import text

    from .db import engine

    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError:
        return JSONResponse(status_code=503, content={"status": "not_ready"})
    return JSONResponse(status_code=200, content={"status": "ready"})

@app.get("/metrics", include_in_schema=False)
def metrics(request: Request) -> PlainTextResponse:
    authorize_metrics(request.headers.get("authorization"))
    return PlainTextResponse(
        request.app.state.metrics.render(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )

@app.get("/api/v1/protected-ping")
def protected_ping(_: Annotated[None, Depends(require_token)]) -> dict[str, str]:
    return {"status": "ok"}
