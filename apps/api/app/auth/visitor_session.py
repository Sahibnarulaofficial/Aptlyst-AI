"""Cryptographic anonymous visitor-session mechanism.

Security Architecture & Rotation Policy:
----------------------------------------
1. Mechanism:
   Anonymous visitor sessions are cryptographically signed using HMAC-SHA256 (HS256).
   Each session token contains:
     - 'sub': Unique visitor identifier (UUID hex string).
     - 'bot_id': UUID of the chatbot this session is bound to.
     - 'conv_id': UUID of the persistent conversation this session owns.
     - 'iat': Unix epoch timestamp of issuance.
     - 'exp': Unix epoch timestamp of expiration (default 7 days).
     - 'aud': Fixed audience 'aptlyst_visitor'.
     - 'iss': Fixed issuer 'aptlyst_api'.

2. Scoping & Multi-Tenant Boundary:
   Visitor session tokens are strictly scoped to a specific (bot_id, conversation_id)
   tuple. A client holding a valid token for Conversation A cannot inspect or append
   messages to Conversation B, even within the same chatbot or organization.

3. Secret Management & Key Rotation:
   The signing secret is managed server-side via `settings.resolved_visitor_session_secret`
   (configured via the `VISITOR_SESSION_SECRET` environment variable).
   - Production deployments MUST provide a strong, high-entropy secret.
   - Key Rotation Procedure:
     To rotate the visitor session secret with minimal disruption:
     a. Deploy a dual-secret verification phase where newly generated tokens use the new
        secret (PRIMARY), while tokens signed by the prior secret (SECONDARY) are accepted
        until their expiration (TTL = 7 days).
     b. Once the 7-day grace window elapses, retire the secondary secret.
   - Never log or expose raw session tokens or signing secrets.
"""

import time
import uuid
from dataclasses import dataclass

import jwt
from fastapi import HTTPException, Request, status

from app.config import settings

VISITOR_AUDIENCE = "aptlyst_visitor"
VISITOR_ISSUER = "aptlyst_api"
VISITOR_ALGORITHM = "HS256"


@dataclass(frozen=True)
class VisitorSession:
    """Validated visitor session information extracted from a verified token."""

    visitor_id: str
    bot_id: uuid.UUID
    conversation_id: uuid.UUID
    expires_at: int


def create_visitor_token(
    bot_id: uuid.UUID,
    conversation_id: uuid.UUID,
    visitor_id: str | None = None,
) -> tuple[str, str]:
    """Generate a signed visitor session token.

    Returns:
        tuple[str, str]: (session_token, visitor_id)
    """
    resolved_visitor_id = visitor_id.strip() if visitor_id and visitor_id.strip() else f"anon_{uuid.uuid4().hex}"
    now = int(time.time())
    exp = now + settings.visitor_session_ttl_seconds

    claims = {
        "sub": resolved_visitor_id,
        "bot_id": str(bot_id),
        "conv_id": str(conversation_id),
        "iat": now,
        "exp": exp,
        "aud": VISITOR_AUDIENCE,
        "iss": VISITOR_ISSUER,
    }

    token = jwt.encode(
        claims,
        settings.resolved_visitor_session_secret,
        algorithm=VISITOR_ALGORITHM,
    )
    return token, resolved_visitor_id


def verify_visitor_token(
    token: str,
    expected_bot_id: uuid.UUID,
    expected_conversation_id: uuid.UUID,
) -> VisitorSession:
    """Decode and verify an anonymous visitor session token.

    Validates:
    - Token integrity (HMAC-SHA256 signature).
    - Expiration (rejects expired tokens).
    - Audience and issuer.
    - Path binding: matches expected_bot_id and expected_conversation_id.

    Raises:
        HTTPException: 401 if token is missing, expired, or invalid.
        HTTPException: 403 if token claims do not match the target bot or conversation.
    """
    claims = None
    for secret in settings.verification_visitor_session_secrets:
        try:
            claims = jwt.decode(
                token,
                secret,
                algorithms=[VISITOR_ALGORITHM],
                audience=VISITOR_AUDIENCE,
                issuer=VISITOR_ISSUER,
                options={"require": ["sub", "bot_id", "conv_id", "exp", "iat", "aud", "iss"]},
            )
            break
        except jwt.ExpiredSignatureError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Visitor session expired",
                headers={"WWW-Authenticate": "Bearer error=\"invalid_token\", error_description=\"The token has expired\""},
            )
        except jwt.InvalidSignatureError:
            # Try next secret in verification_visitor_session_secrets (for rotation grace periods)
            continue
        except jwt.PyJWTError:
            break

    if claims is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid visitor session token",
            headers={"WWW-Authenticate": "Bearer error=\"invalid_token\""},
        )

    # Verify bot binding
    claim_bot_id = claims.get("bot_id")
    if claim_bot_id != str(expected_bot_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Session token does not match requested chatbot",
        )

    # Verify conversation binding
    claim_conv_id = claims.get("conv_id")
    if claim_conv_id != str(expected_conversation_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Session token does not match requested conversation",
        )

    visitor_id = claims.get("sub", "")
    exp = int(claims.get("exp", 0))

    return VisitorSession(
        visitor_id=visitor_id,
        bot_id=expected_bot_id,
        conversation_id=expected_conversation_id,
        expires_at=exp,
    )


async def get_verified_visitor_session(
    bot_id: uuid.UUID,
    conversation_id: uuid.UUID,
    request: Request,
) -> VisitorSession:
    """FastAPI dependency to extract and verify the visitor session from headers.

    Extracts token from either:
    1. 'Authorization: Bearer <token>'
    2. 'X-Visitor-Session: <token>'
    """
    raw_token: str | None = None

    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.strip().lower().startswith("bearer "):
        raw_token = auth_header.strip()[7:].strip()
    elif request.headers.get("X-Visitor-Session"):
        raw_token = request.headers.get("X-Visitor-Session", "").strip()

    if not raw_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing visitor session token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return verify_visitor_token(
        token=raw_token,
        expected_bot_id=bot_id,
        expected_conversation_id=conversation_id,
    )
