"""FastAPI dependency for validating Supabase JWT access tokens.

Supports both:
- Asymmetric ES256 (modern Supabase default): fetches public keys from Supabase's
  published JWKS endpoint (.well-known/jwks.json) derived from SUPABASE_URL,
  caching keys in-memory with automatic key rotation support.
- Symmetric HS256: verifies tokens using SUPABASE_JWT_SECRET when explicitly configured.

The verified token yields a CurrentUser with the authenticated Supabase user ID
and email. Business authorization (org membership, role checks) is handled
separately in org-level dependencies.
"""

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Annotated, Any

import httpx
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import settings

logger = logging.getLogger(__name__)

_bearer = HTTPBearer(auto_error=True)


@dataclass(frozen=True)
class CurrentUser:
    id: uuid.UUID
    email: str | None


class JWKSCache:
    """In-memory cache for Supabase JWKS public keys.

    Caches keys to avoid per-request network calls, with support for key rotation
    and rate-limited refreshing to prevent DoS.
    """

    def __init__(self, ttl: float = 300.0, min_refresh_interval: float = 10.0) -> None:
        self.ttl = ttl
        self.min_refresh_interval = min_refresh_interval
        self._jwk_set: jwt.PyJWKSet | None = None
        self._last_fetched: float = 0.0
        self._last_network_fetch: float = 0.0
        self._lock = asyncio.Lock()

    def clear(self) -> None:
        """Clear cached keys (useful for testing key rotation)."""
        self._jwk_set = None
        self._last_fetched = 0.0
        self._last_network_fetch = 0.0

    def set_jwks_data(self, jwks_data: dict[str, Any]) -> None:
        """Manually populate cache (useful for testing without network calls)."""
        self._jwk_set = jwt.PyJWKSet.from_dict(jwks_data)
        self._last_fetched = time.time()

    async def get_key_for_kid(
        self,
        jwks_url: str,
        kid: str,
        http_client: httpx.AsyncClient | None = None,
    ) -> Any:
        now = time.time()

        # 1. Fast path: check in-memory cache if key exists and cache is not expired
        if self._jwk_set is not None:
            try:
                jwk = self._jwk_set[kid]
                if now - self._last_fetched < self.ttl:
                    return jwk.key
            except (KeyError, jwt.PyJWKSetError):
                pass

        # 2. Key not in cache or cache expired: acquire lock and refresh
        async with self._lock:
            now = time.time()
            if self._jwk_set is not None:
                try:
                    jwk = self._jwk_set[kid]
                    if now - self._last_fetched < self.ttl:
                        return jwk.key
                except (KeyError, jwt.PyJWKSetError):
                    pass

            # Rate-limit network refresh attempts to prevent DoS from invalid kid spam
            if now - self._last_network_fetch < self.min_refresh_interval:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Unknown key identifier",
                    headers={"WWW-Authenticate": "Bearer"},
                )

            # Fetch JWKS from the endpoint
            try:
                data = await self._fetch_jwks(jwks_url, http_client=http_client)
                self._jwk_set = jwt.PyJWKSet.from_dict(data)
                self._last_fetched = now
                self._last_network_fetch = now
            except HTTPException:
                self._last_network_fetch = now
                raise
            except Exception as exc:
                self._last_network_fetch = now
                logger.warning("Failed to fetch or parse JWKS: %s", exc)
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Authentication service unavailable",
                    headers={"WWW-Authenticate": "Bearer"},
                )

            # Look up key in the refreshed set
            try:
                jwk = self._jwk_set[kid]
                return jwk.key
            except (KeyError, jwt.PyJWKSetError):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Unknown key identifier",
                    headers={"WWW-Authenticate": "Bearer"},
                )

    async def _fetch_jwks(
        self,
        jwks_url: str,
        http_client: httpx.AsyncClient | None = None,
    ) -> dict[str, Any]:
        """Fetch raw JWKS data from the Supabase endpoint."""
        try:
            if http_client is not None:
                response = await http_client.get(jwks_url, timeout=5.0)
            else:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    response = await client.get(jwks_url)

            if response.status_code != 200:
                logger.warning("JWKS endpoint returned status %s", response.status_code)
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Authentication service unavailable",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            return response.json()
        except HTTPException:
            raise
        except Exception as exc:
            logger.warning("Error communicating with JWKS endpoint: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication service unavailable",
                headers={"WWW-Authenticate": "Bearer"},
            )


default_jwks_cache = JWKSCache()


async def _verify_token(
    token: str,
    jwks_cache: JWKSCache | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> CurrentUser:
    """Decode and verify a Supabase JWT. Raises HTTPException on failure."""
    algorithm = (
        settings.supabase_jwt_algorithm
        if isinstance(getattr(settings, "supabase_jwt_algorithm", None), str)
        else "ES256"
    )
    if algorithm.lower() == "none":
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Insecure JWT algorithm configured",
        )

    audience = (
        settings.supabase_jwt_audience
        if isinstance(getattr(settings, "supabase_jwt_audience", None), str)
        else "authenticated"
    )

    try:
        header = jwt.get_unverified_header(token)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token header",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token_alg = header.get("alg")
    if token_alg != algorithm:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token algorithm",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if algorithm == "ES256":
        supabase_url = getattr(settings, "supabase_url", None)
        if not isinstance(supabase_url, str) or not supabase_url.strip():
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Authentication service misconfigured: missing Supabase URL",
            )
        kid = header.get("kid")
        if not kid or not isinstance(kid, str):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token missing key identifier (kid)",
                headers={"WWW-Authenticate": "Bearer"},
            )
        cache = jwks_cache or default_jwks_cache
        jwks_url = f"{supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json"
        verification_key = await cache.get_key_for_kid(jwks_url, kid, http_client=http_client)
    elif algorithm == "HS256":
        secret = getattr(settings, "supabase_jwt_secret", None)
        if not isinstance(secret, str) or not secret.strip():
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Authentication service misconfigured: missing JWT secret",
            )
        verification_key = secret
    else:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Unsupported JWT algorithm configured: {algorithm}",
        )

    try:
        payload = jwt.decode(
            token,
            verification_key,
            algorithms=[algorithm],
            audience=audience,
            options={
                "require": ["sub", "exp", "aud"],
            },
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    role: str | None = payload.get("role")
    if role != "authenticated":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token role",
            headers={"WWW-Authenticate": "Bearer"},
        )

    sub: str | None = payload.get("sub")
    if not sub:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token missing subject",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        user_id = uuid.UUID(sub)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid subject format",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return CurrentUser(
        id=user_id,
        email=payload.get("email"),
    )


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(_bearer)],
) -> CurrentUser:
    """FastAPI dependency: validates the Bearer token and returns the current user."""
    return await _verify_token(credentials.credentials)


# Convenient type alias for use in route signatures
AuthenticatedUser = Annotated[CurrentUser, Depends(get_current_user)]
