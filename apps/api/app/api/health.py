import logging
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.services.billing.webhook_errors import sanitize_billing_error_message

logger = logging.getLogger("app.api.health")

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    """Basic liveness check – confirms the API process is running."""
    return {"status": "ok"}


@router.get("/health/db")
async def health_db(db: Annotated[AsyncSession, Depends(get_db)]) -> JSONResponse:
    """Database connectivity check – executes a lightweight query against PostgreSQL."""
    try:
        await db.execute(text("SELECT 1"))
        return JSONResponse({"status": "ok", "database": "connected"})
    except Exception as exc:
        logger.warning(
            "Database health check failed (%s): %s",
            type(exc).__name__,
            sanitize_billing_error_message(exc),
        )
        return JSONResponse(
            {"status": "error", "database": "unavailable"},
            status_code=503,
        )
