"""PostgreSQL transaction-scoped advisory locking for Razorpay entities.

Guarantees & Invariants:
1. Deterministic Key Derivation:
   - Derives a signed 64-bit integer from SHA-256("razorpay:<clean_type>:<clean_id>").
   - Strictly avoids Python process-randomized hash().
   - The same entity always yields the exact same key across processes, runs, and platforms.
   - Different entities yield distinct keys with negligible collision probability (2^-64).
2. Transaction-Scoped Semantics:
   - Uses PostgreSQL pg_advisory_xact_lock(:key).
   - Automatically released upon transaction commit or rollback.
   - No session-level lock leaks on worker crash or unhandled exception.
3. Safe Handling of Missing Identifiers:
   - Missing or empty entity_type or entity_id raises ValueError immediately.
   - Never acquires a lock on empty strings or null keys.
4. Consistent Lock Order (Deadlock Avoidance):
   - Advisory lock (Level 0) must be acquired before domain table row locks:
     Level 0: pg_advisory_xact_lock
     Level 1: OrganizationBillingPeriod (FOR UPDATE)
     Level 2: Subscription (FOR UPDATE)
     Level 3: BillingInvoice (FOR UPDATE)
     Level 4: BillingPayment (FOR UPDATE)
5. Zero Network Calls Under Lock:
   - External gateway network calls are never made while advisory locks or DB transactions are held.
6. Deadlock Considerations:
   - While advisory locks serialize concurrent events for the same entity, global deadlock freedom
     cannot be theoretically claimed for cross-entity operations. Lock timeouts and strict ordering
     are required.
"""

from datetime import datetime, timezone
import hashlib
import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# Valid signed 64-bit integer range in PostgreSQL: [-2^63, 2^63 - 1]
PG_BIGINT_MIN: int = -(2**63)
PG_BIGINT_MAX: int = 2**63 - 1

# Explicit whitelist of allowed entity categories for advisory locking
ALLOWED_ENTITY_TYPES: frozenset[str] = frozenset({
    "subscription",
    "payment",
    "invoice",
    "customer",
})


def derive_razorpay_advisory_lock_key(entity_type: str, entity_id: str) -> int:
    """Derive a deterministic signed 64-bit integer for PostgreSQL advisory locking.

    Format: SHA-256("razorpay:<clean_type>:<clean_id>")[:8] as big-endian signed 64-bit int.

    Args:
        entity_type: Category of entity (must be in ALLOWED_ENTITY_TYPES).
        entity_id: Authoritative gateway entity ID (e.g. 'sub_123', 'pay_456').

    Returns:
        int: Signed 64-bit integer in range [-2^63, 2^63 - 1].

    Raises:
        ValueError: If entity_type or entity_id is missing, empty, or entity_type not whitelisted.
    """
    if not entity_type or not isinstance(entity_type, str) or not entity_type.strip():
        raise ValueError("entity_type must be a non-empty string.")
    if not entity_id or not isinstance(entity_id, str) or not entity_id.strip():
        raise ValueError("entity_id must be a non-empty string.")

    clean_type = entity_type.strip().lower()
    if clean_type not in ALLOWED_ENTITY_TYPES:
        raise ValueError(
            f"Invalid entity_type '{entity_type}'. Must be one of: {sorted(ALLOWED_ENTITY_TYPES)}"
        )

    clean_id = entity_id.strip()

    raw = f"razorpay:{clean_type}:{clean_id}".encode("utf-8")
    digest = hashlib.sha256(raw).digest()
    key = int.from_bytes(digest[:8], byteorder="big", signed=True)

    # Sanity bounds check
    if not (PG_BIGINT_MIN <= key <= PG_BIGINT_MAX):
        raise OverflowError(f"Derived key {key} is out of bounds for PostgreSQL 64-bit signed integer.")

    return key


async def acquire_razorpay_advisory_lock(
    db: AsyncSession,
    entity_type: str,
    entity_id: str,
) -> int:
    """Acquire a transaction-scoped exclusive advisory lock for a Razorpay entity.

    Executes `SELECT pg_advisory_xact_lock(:key)` within the active database transaction.
    The lock is released automatically when the current transaction commits or rolls back.

    Args:
        db: Active SQLAlchemy AsyncSession.
        entity_type: Entity category (e.g. 'subscription', 'payment', 'invoice').
        entity_id: Entity gateway identifier (e.g. 'sub_123').

    Returns:
        int: The acquired 64-bit advisory lock key.
    """
    key = derive_razorpay_advisory_lock_key(entity_type, entity_id)
    logger.debug(
        "Acquiring PostgreSQL transaction advisory lock for entity %s:%s (key=%d)",
        entity_type,
        entity_id,
        key,
    )
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
    return key


async def try_acquire_razorpay_advisory_lock(
    db: AsyncSession,
    entity_type: str,
    entity_id: str,
) -> bool:
    """Attempt to acquire a transaction-scoped advisory lock without blocking.

    Executes `SELECT pg_try_advisory_xact_lock(:key)`.

    Returns:
        bool: True if lock was acquired, False if held by another transaction.
    """
    key = derive_razorpay_advisory_lock_key(entity_type, entity_id)
    res = await db.execute(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": key})
    return bool(res.scalar())
