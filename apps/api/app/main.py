from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from app.api.chatbots import router as chatbots_router
from app.api.health import router as health_router
from app.api.organizations import router as organizations_router
from app.api.public_chatbots import router as public_chatbots_router

app = FastAPI(title="Aptlyst AI API", version="0.1.0")

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
