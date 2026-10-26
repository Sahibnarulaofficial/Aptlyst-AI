"""Rate limiting and abuse protection for public chatbot endpoints.

Architecture:
- Pluggable backends: InMemoryRateLimiter and RedisRateLimiter.
- Explicit sliding-window counter algorithm (no ambiguous burst buffers).
- Atomic concurrency safety: asyncio.Lock for in-memory, Lua scripts for Redis.
- Bounded in-memory storage with hard capacity ceiling and defined overflow policies.
- Secure client IP resolution validating immediate socket peers and reverse proxies right-to-left.
- Standardized HTTP 429 response with Retry-After and rate limit headers.
- Privacy-conscious logging of rate-limit events.
- Transparent and logged fallback behavior when Redis is unreachable.
"""

import asyncio
import ipaddress
import logging
import math
import re
import time
from abc import ABC, abstractmethod
from collections import OrderedDict
from typing import Any

from fastapi import HTTPException, Request, status

from app.config import settings

logger = logging.getLogger(__name__)


class RateLimitExceeded(HTTPException):
    """Exception raised when a rate limit threshold is exceeded."""

    def __init__(self, limit: str, retry_after: int, reset: int):
        super().__init__(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded. Please wait before submitting another request.",
            headers={
                "Retry-After": str(retry_after),
                "X-RateLimit-Limit": limit,
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": str(reset),
            },
        )
        self.retry_after = retry_after
        self.limit = limit
        self.reset = reset


def mask_ip_for_logging(ip: str) -> str:
    """Mask IP for privacy-conscious logs (GDPR/CCPA compliant)."""
    if ":" in ip:
        parts = ip.split(":")
        return ":".join(parts[:3]) + ":****" if len(parts) >= 3 else "****"
    parts = ip.split(".")
    if len(parts) == 4:
        return f"{parts[0]}.{parts[1]}.{parts[2]}.***"
    return "***"


def _sanitize_redis_error(exc: Exception) -> str:
    """Sanitize Redis error messages to avoid exposing credentials in application logs."""
    msg = str(exc)
    return re.sub(r"://([^:]+):([^@]+)@", r"://\1:***@", msg)


def get_client_ip(request: Request) -> str:
    """Securely resolve the client IP address.

    Security Strategy:
    1. Inspect direct TCP socket peer (`request.client.host`).
    2. If direct peer is NOT in `settings.parsed_trusted_proxy_networks`, ignore ALL
       forwarding headers (X-Forwarded-For, X-Real-IP) and return direct peer.
    3. If direct peer IS a trusted proxy:
       - Parse `X-Forwarded-For` from right to left.
       - Discard trusted proxy hops and invalid IP strings.
       - Return the rightmost untrusted IP address.
       - If no untrusted IP is found in X-Forwarded-For, check X-Real-IP.
       - Fallback to direct peer.
    """
    raw_peer = request.client.host if request.client else "127.0.0.1"
    try:
        peer_ip = ipaddress.ip_address(raw_peer)
    except ValueError:
        return "127.0.0.1"

    trusted_networks = settings.parsed_trusted_proxy_networks
    is_peer_trusted = any(peer_ip in net for net in trusted_networks)

    if not is_peer_trusted:
        # Untrusted socket peer: ignore client-controlled forwarding headers
        return str(peer_ip)

    # Peer is trusted (e.g. reverse proxy / WAF). Inspect X-Forwarded-For right-to-left
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        raw_ips = [item.strip() for item in forwarded_for.split(",") if item.strip()]
        for candidate_str in reversed(raw_ips):
            try:
                candidate_ip = ipaddress.ip_address(candidate_str)
            except ValueError:
                continue
            if not any(candidate_ip in net for net in trusted_networks):
                return str(candidate_ip)

    # Check X-Real-IP header if X-Forwarded-For did not yield an untrusted candidate
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        try:
            candidate_ip = ipaddress.ip_address(real_ip.strip())
            if not any(candidate_ip in net for net in trusted_networks):
                return str(candidate_ip)
        except ValueError:
            pass

    return str(peer_ip)


class BaseRateLimiter(ABC):
    """Abstract rate limiter backend."""

    @abstractmethod
    async def check_rate_limit(
        self, key: str, limit: int, window_seconds: int, now: float | None = None
    ) -> tuple[bool, int, int]:
        """Check rate limit for the given key and sliding window.

        Returns:
            tuple[bool, int, int]: (allowed, remaining, retry_after)
        """
        pass


class RateLimitEntry:
    """Stored sliding-window state for an in-memory rate-limited key."""

    __slots__ = ("window_seconds", "timestamps")

    def __init__(self, window_seconds: int, timestamps: list[float] | None = None):
        self.window_seconds = window_seconds
        self.timestamps: list[float] = timestamps or []


class InMemoryRateLimiter(BaseRateLimiter):
    """Thread/coroutine safe in-memory sliding window rate limiter with bounded storage.

    Security & Operational Characteristics:
    - Sliding-window precision without burst/bucket boundary artifacts.
    - Bounded memory footprint (up to rate_limit_max_keys entries).
    - Heterogeneous window retention: each entry retains its specific window duration
      so that high-frequency checks (e.g. 60s) never purge active long-duration
      counters (e.g. 3600s).
    - LRU eviction protection: actively touched keys (both allowed and rejected)
      are preserved at the MRU end so attackers cannot force LRU eviction of their
      own blocked counters.
    - Cardinality Note: In-memory rate limiting is fundamentally a single-process,
      best-effort protection layer. Under high-cardinality distributed attacks
      (e.g. millions of unique botnet IPs), local memory will saturate. For
      multi-worker distributed environments and enterprise DDoS protection,
      configure Redis storage and edge WAF / reverse-proxy filtering.
    """

    def __init__(self, max_keys: int | None = None, overflow_policy: str | None = None):
        self._lock = asyncio.Lock()
        self._storage: OrderedDict[str, RateLimitEntry] = OrderedDict()
        self._max_keys = max_keys or settings.rate_limit_max_keys
        self._overflow_policy = overflow_policy or settings.rate_limit_capacity_overflow_policy

    def clear(self) -> None:
        """Clear all stored keys and reset lock (useful for test isolation)."""
        self._storage.clear()
        self._lock = asyncio.Lock()

    async def check_rate_limit(
        self, key: str, limit: int, window_seconds: int, now: float | None = None
    ) -> tuple[bool, int, int]:
        async with self._lock:
            current_time = now if now is not None else time.monotonic()

            entry = self._storage.get(key)
            if entry is not None:
                # Update window duration if changed, and prune expired timestamps
                entry.window_seconds = window_seconds
                window_start = current_time - window_seconds
                entry.timestamps = [ts for ts in entry.timestamps if ts > window_start]
            else:
                # Check storage capacity before adding new key
                if len(self._storage) >= self._max_keys:
                    # Run cleanup pass to remove genuinely expired keys across heterogeneous windows
                    keys_to_remove = [
                        k
                        for k, e in self._storage.items()
                        if not e.timestamps or e.timestamps[-1] <= current_time - e.window_seconds
                    ]
                    for k in keys_to_remove:
                        del self._storage[k]

                    # If still at or above capacity, apply overflow policy
                    if len(self._storage) >= self._max_keys:
                        if self._overflow_policy == "drop_new":
                            logger.warning(
                                "Rate limiter in-memory capacity reached (%d keys). Rejecting new key under 'drop_new' policy.",
                                self._max_keys,
                            )
                            return False, 0, window_seconds

                        # Default policy: evict oldest LRU key
                        evicted_key, _ = self._storage.popitem(last=False)
                        logger.warning(
                            "Rate limiter in-memory capacity reached (%d keys). Evicted oldest LRU key '%s'.",
                            self._max_keys,
                            evicted_key,
                        )

                entry = RateLimitEntry(window_seconds=window_seconds)
                self._storage[key] = entry

            # Maintain LRU order: touch key to MRU end for BOTH allowed and rejected requests
            self._storage.move_to_end(key)

            current_count = len(entry.timestamps)
            if current_count < limit:
                entry.timestamps.append(current_time)
                remaining = limit - current_count - 1
                return True, remaining, 0
            else:
                oldest_in_window = entry.timestamps[0]
                retry_after = max(1, math.ceil(oldest_in_window + window_seconds - current_time))
                return False, 0, retry_after


# Redis Lua Script for atomic sliding window enforcement
REDIS_SLIDING_WINDOW_LUA = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local clear_before = now - window

-- 1. Remove expired entries
redis.call('ZREMRANGEBYSCORE', key, '-inf', clear_before)

-- 2. Count active entries in current window
local current_count = redis.call('ZCARD', key)

if current_count < limit then
    -- Add current timestamp with unique tiebreaker
    redis.call('ZADD', key, now, tostring(now) .. ':' .. tostring(current_count))
    redis.call('EXPIRE', key, math.ceil(window) + 1)
    return {1, limit - current_count - 1, 0}
else
    -- Find oldest entry to compute retry_after
    local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
    local oldest_time = now
    if #oldest >= 2 then
        oldest_time = tonumber(oldest[2])
    end
    local retry_after = math.max(1, math.ceil(oldest_time + window - now))
    return {0, 0, retry_after}
end
"""


def validate_rate_limiter_configuration() -> None:
    """Validate rate limiter configuration at application startup.

    Fails fast with RuntimeError if Redis storage is explicitly configured
    but either REDIS_URL is missing or the 'redis' package is not installed.
    This prevents silent, uninspected degradation to in-memory mode when operators
    expect distributed enforcement.
    """
    if not settings.rate_limiting_enabled:
        return
    if settings.rate_limit_storage.strip().lower() == "redis":
        if not settings.redis_url.strip():
            raise RuntimeError(
                "RATE_LIMIT_STORAGE is set to 'redis', but REDIS_URL is not configured."
            )
        try:
            import redis.asyncio as aioredis  # noqa: F401
        except ImportError:
            raise RuntimeError(
                "RATE_LIMIT_STORAGE is set to 'redis', but the 'redis' Python package is not installed. "
                "Install 'redis' in your environment or set RATE_LIMIT_STORAGE=memory."
            )


class RedisRateLimiter(BaseRateLimiter):
    """Shared Redis-backed rate limiter using atomic Lua scripts with fallback policies."""

    def __init__(
        self,
        redis_client: Any | None = None,
        fallback_limiter: BaseRateLimiter | None = None,
        fallback_policy: str | None = None,
    ):
        self._redis = redis_client
        self._fallback_limiter = fallback_limiter or InMemoryRateLimiter()
        self._fallback_policy = fallback_policy or settings.redis_fallback_policy
        self._lua_script: Any = None

    async def _get_redis_client(self) -> Any:
        if self._redis is not None:
            return self._redis

        validate_rate_limiter_configuration()

        import redis.asyncio as aioredis  # type: ignore[import-untyped]

        self._redis = aioredis.from_url(
            settings.redis_url,
            socket_connect_timeout=settings.redis_connect_timeout_seconds,
            socket_timeout=settings.redis_command_timeout_seconds,
            decode_responses=True,
        )
        return self._redis

    async def close(self) -> None:
        """Close underlying Redis client and connection pool."""
        if self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception:
                pass
            self._redis = None
            self._lua_script = None

    async def check_rate_limit(
        self, key: str, limit: int, window_seconds: int, now: float | None = None
    ) -> tuple[bool, int, int]:
        redis_key = f"rate_limit:{key}"
        current_time = now if now is not None else time.time()

        try:
            client = await self._get_redis_client()
            if self._lua_script is None:
                self._lua_script = client.register_script(REDIS_SLIDING_WINDOW_LUA)

            # Returns {allowed, remaining, retry_after}
            result = await self._lua_script(
                keys=[redis_key],
                args=[current_time, window_seconds, limit],
            )
            allowed = bool(result[0])
            remaining = int(result[1])
            retry_after = int(result[2])
            return allowed, remaining, retry_after

        except Exception as exc:
            sanitized_exc = _sanitize_redis_error(exc)
            # Handle runtime failure according to configured policy
            if self._fallback_policy == "memory":
                logger.warning(
                    "Redis rate limiter unavailable (%s); falling back to in-memory rate limiting. "
                    "Note: Rate limits are enforced per-process, NOT distributed across multiple instances.",
                    sanitized_exc,
                )
                return await self._fallback_limiter.check_rate_limit(
                    key, limit, window_seconds, now=now
                )
            elif self._fallback_policy == "fail_open":
                logger.warning(
                    "Redis rate limiter unavailable (%s); failing open (request allowed).",
                    sanitized_exc,
                )
                return True, 1, 0
            else:  # fail_closed
                logger.error(
                    "Redis rate limiter unavailable (%s); failing closed (request rejected).",
                    sanitized_exc,
                )
                return False, 0, window_seconds


# Global singleton instances
_in_memory_limiter = InMemoryRateLimiter()
_redis_limiter: RedisRateLimiter | None = None


def get_rate_limiter() -> BaseRateLimiter:
    """Return the configured rate limiter instance."""
    global _redis_limiter
    if settings.rate_limit_storage.strip().lower() == "redis":
        validate_rate_limiter_configuration()
        if _redis_limiter is None:
            _redis_limiter = RedisRateLimiter(fallback_limiter=_in_memory_limiter)
        return _redis_limiter
    return _in_memory_limiter


async def check_rate_limit(
    key: str,
    limit: int,
    window_seconds: int,
    limit_name: str,
    request: Request | None = None,
) -> None:
    """Check rate limit and raise RateLimitExceeded (HTTP 429) if threshold is reached."""
    if not settings.rate_limiting_enabled:
        return

    limiter = get_rate_limiter()
    allowed, remaining, retry_after = await limiter.check_rate_limit(
        key=key, limit=limit, window_seconds=window_seconds
    )

    if not allowed:
        client_ip = get_client_ip(request) if request else "unknown"
        logger.warning(
            "Rate limit '%s' exceeded for client %s on key '%s'. Retry-After: %ds.",
            limit_name,
            mask_ip_for_logging(client_ip),
            key,
            retry_after,
        )
        reset_timestamp = int(time.time() + retry_after)
        window_desc = f"{window_seconds}s" if window_seconds != 60 else "minute"
        if window_seconds == 3600:
            window_desc = "hour"
        raise RateLimitExceeded(
            limit=f"{limit}/{window_desc}",
            retry_after=retry_after,
            reset=reset_timestamp,
        )
