from contextlib import asynccontextmanager
import logging
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.billing import router as billing_router, webhook_router as billing_webhook_router
from app.api.chatbots import router as chatbots_router
from app.api.health import router as health_router
from app.api.organizations import router as organizations_router
from app.api.public_chatbots import router as public_chatbots_router
from app.config import settings
from app.security.rate_limiter import (
    RateLimitExceeded,
    get_rate_limiter,
    validate_rate_limiter_configuration,
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan managing startup validation and resource cleanup."""
    validate_rate_limiter_configuration()
    if settings.default_free_monthly_token_quota <= 0:
        logger.warning(
            "DEFAULT_FREE_MONTHLY_TOKEN_QUOTA is configured to %d. "
            "New Free organizations will fail LLM token reservations with QUOTA_EXHAUSTED "
            "until a positive quota is configured via environment variables.",
            settings.default_free_monthly_token_quota,
        )
    if settings.stripe_enabled and settings.default_pro_monthly_token_quota <= 0:
        logger.warning(
            "DEFAULT_PRO_MONTHLY_TOKEN_QUOTA is configured to %d. "
            "Paid Pro invoices will be quarantined with missing_pro_quota_configuration "
            "until a positive quota is configured via environment variables.",
            settings.default_pro_monthly_token_quota,
        )
    yield
    limiter = get_rate_limiter()
    if hasattr(limiter, "close"):
        await limiter.close()


app = FastAPI(title="Aptlyst AI API", version="0.1.0", lifespan=lifespan)


@app.exception_handler(RateLimitExceeded)
async def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    """Format HTTP 429 response with consistent JSON body and rate-limit headers."""
    response = JSONResponse(
        status_code=exc.status_code,
        content={
            "detail": exc.detail,
            "retry_after": exc.retry_after,
            "limit": exc.limit,
        },
        headers=dict(exc.headers or {}),
    )
    if request.url.path.startswith("/api/v1/public/"):
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Expose-Headers"] = (
            "Retry-After, X-RateLimit-Limit, X-RateLimit-Remaining, X-RateLimit-Reset"
        )
    return response

# Internal dashboard CORS: restricted to authorized frontend origins with credentials.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def public_cors_middleware(request: Request, call_next):
    """CORS middleware for public runtime endpoints.

    Security note:
    CORS (Cross-Origin Resource Sharing) is a browser mechanism that restricts
    cross-origin reads; it does NOT provide authentication, authorization, or
    abuse prevention.
    Public endpoints (/api/v1/public/*) are unauthenticated by design to serve
    embeddable widgets across external customer domains. They strictly expose
    safe read-only visual configurations (id, name, welcome_message, primary_color)
    and must rely on input validation, status enforcement (active-only), and future
    IP rate-limiting/abuse prevention before any LLM inference endpoint is added.
    """
    if request.url.path.startswith("/api/v1/public/"):
        if request.method == "OPTIONS":
            response = Response(status_code=204)
            response.headers["Access-Control-Allow-Origin"] = "*"
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
            response.headers["Access-Control-Allow-Headers"] = "Content-Type, Accept, Authorization, X-Visitor-Session"
            return response

        response = await call_next(request)
        response.headers["Access-Control-Allow-Origin"] = "*"
        return response

    return await call_next(request)


app.include_router(health_router)
app.include_router(organizations_router, prefix="/api/v1")
app.include_router(chatbots_router, prefix="/api/v1")
app.include_router(public_chatbots_router, prefix="/api/v1")
app.include_router(billing_router, prefix="/api/v1")
app.include_router(billing_webhook_router, prefix="/api/v1")
