"""PostgreSQL transaction-scoped advisory locking primitives for Aptlyst AI.

Guarantees & Architectural Contracts:
1. Deterministic Key Derivation:
   - Derives a signed 64-bit integer from SHA-256(raw_key)[:8] in big-endian byte order.
   - Output range is strictly [-2^63, 2^63 - 1] (matching PostgreSQL 'bigint').
   - Independent of Python process hash seeds (hashlib.sha256).
   - Collision-resistant (keyspace of 2^64 values).

2. Domain Normalization Rules:
   - Razorpay entities ("subscription", "payment", "invoice", "customer"):
     Format: SHA-256("razorpay:<normalized_type>:<trimmed_id>")[:8]
     entity_type is lowercased and stripped; entity_id is trimmed, preserving case.
   - Stripe invoices ("stripe_invoice"):
     Format: SHA-256("stripe_invoice:<raw_invoice_id>")[:8]
     invoice_id is preserved exactly as received (raw, case-sensitive).
   - UUID-backed entities ("organization", "user"):
     Format: SHA-256("<normalized_namespace>:<canonical_uuid>")[:8]
     namespace is lowercased and stripped; identifier is parsed and formatted as
     canonical lowercase hyphenated UUID string.

3. Transaction-Scoped Semantics:
   - Uses PostgreSQL `pg_advisory_xact_lock(:key)`.
   - Bound strictly to the active database transaction; automatically released on
     COMMIT or ROLLBACK.
   - Zero risk of connection-leak across connection pool recycles.

4. Multi-Entity Monotonic Acquisition:
   - Deduplicates logical requests.
   - Detects distinct logical resources that map to identical 64-bit integer keys
     (hash collision) and logs a structured warning.
   - Sorts unique keys in strictly ascending signed integer order before execution
     to eliminate circular-wait deadlock cycles under consistent global ordering.

5. Global Lock Hierarchy (ADR-005):
   Level 0A: Organization Advisory Lock (`organization:<uuid>`)
   Level 0B: Entity Advisory Lock (`razorpay:<type>:<id>`, `stripe_invoice:<id>`)
   Level 1:  Billing Period Row Lock (`organization_billing_periods` FOR UPDATE)
   Level 2:  Subscription Row Lock (`subscriptions` FOR UPDATE)
   Level 3:  Auxiliary Row Locks (`billing_invoices`, `billing_payments`, etc. FOR UPDATE)
"""

import hashlib
import logging
from typing import Any
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# Valid signed 64-bit integer range in PostgreSQL: [-2^63, 2^63 - 1]
PG_BIGINT_MIN: int = -(2**63)
PG_BIGINT_MAX: int = 2**63 - 1

# Established Razorpay entity types
RAZORPAY_ENTITY_TYPES: frozenset[str] = frozenset({
    "subscription",
    "payment",
    "invoice",
    "customer",
})

# Namespaces strictly backed by UUID identifiers
UUID_NAMESPACES: frozenset[str] = frozenset({
    "organization",
    "user",
})


def derive_advisory_lock_key(namespace: str, identifier: str | uuid.UUID) -> int:
    """Derive a deterministic signed 64-bit integer for PostgreSQL advisory locking.

    Preserves exact byte-for-byte derivation across all established domains:
    - If namespace in {"subscription", "payment", "invoice", "customer"} or starts with "razorpay:":
        raw = f"razorpay:{clean_type}:{clean_id}" (clean_id is case-preserved)
    - If namespace == "stripe_invoice":
        raw = f"stripe_invoice:{raw_invoice_id}" (raw_invoice_id preserved exactly)
    - If namespace in {"organization", "user"}:
        raw = f"{clean_namespace}:{canonical_uuid_str}" (canonical lowercase hyphenated UUID)
    - Other namespaces:
        raw = f"{clean_namespace}:{clean_id}"

    Args:
        namespace: Category / domain of the lock target.
        identifier: Domain identifier (string or UUID).

    Returns:
        int: Signed 64-bit integer in range [-2^63, 2^63 - 1].

    Raises:
        ValueError: If namespace or identifier is missing, empty, or an invalid UUID.
    """
    if not namespace or not isinstance(namespace, str) or not namespace.strip():
        raise ValueError("namespace must be a non-empty string.")
    if identifier is None:
        raise ValueError("identifier must not be None.")

    clean_ns = namespace.strip().lower()

    # Domain 1: Razorpay entities
    if clean_ns in RAZORPAY_ENTITY_TYPES or clean_ns.startswith("razorpay:"):
        if clean_ns.startswith("razorpay:"):
            raw_type = clean_ns.split(":", 1)[1].strip().lower()
        else:
            raw_type = clean_ns
        if raw_type not in RAZORPAY_ENTITY_TYPES:
            raise ValueError(
                f"Invalid Razorpay entity type '{raw_type}'. Must be one of: {sorted(RAZORPAY_ENTITY_TYPES)}"
            )
        if not isinstance(identifier, str) or not identifier.strip():
            raise ValueError("identifier must be a non-empty string for Razorpay entities.")
        clean_id = identifier.strip()
        raw = f"razorpay:{raw_type}:{clean_id}"

    # Domain 2: Stripe invoices (exact raw string, no strip, no case fold)
    elif clean_ns == "stripe_invoice":
        if not isinstance(identifier, str) or not identifier:
            raise ValueError("identifier must be a non-empty string for stripe_invoice.")
        raw = f"stripe_invoice:{identifier}"

    # Domain 3: UUID-backed entities (organization, user)
    elif clean_ns in UUID_NAMESPACES:
        if isinstance(identifier, uuid.UUID):
            canonical_uuid = str(identifier).lower()
        elif isinstance(identifier, str) and identifier.strip():
            try:
                canonical_uuid = str(uuid.UUID(identifier.strip())).lower()
            except ValueError as err:
                raise ValueError(
                    f"Invalid UUID '{identifier}' for namespace '{clean_ns}': {err}"
                ) from err
        else:
            raise ValueError(
                f"identifier must be a valid UUID or non-empty UUID string for namespace '{clean_ns}'."
            )
        raw = f"{clean_ns}:{canonical_uuid}"

    # Domain 4: General namespaces
    else:
        if isinstance(identifier, uuid.UUID):
            clean_id = str(identifier).lower()
        elif isinstance(identifier, str) and identifier.strip():
            clean_id = identifier.strip()
        else:
            raise ValueError(f"identifier must be a non-empty string or UUID for namespace '{clean_ns}'.")
        raw = f"{clean_ns}:{clean_id}"

    digest = hashlib.sha256(raw.encode("utf-8")).digest()
    key = int.from_bytes(digest[:8], byteorder="big", signed=True)

    if not (PG_BIGINT_MIN <= key <= PG_BIGINT_MAX):
        raise OverflowError(f"Derived key {key} is out of bounds for PostgreSQL 64-bit signed integer.")

    return key


async def acquire_advisory_lock(
    db: AsyncSession,
    namespace: str,
    identifier: str | uuid.UUID,
) -> int:
    """Acquire a transaction-scoped exclusive advisory lock.

    Executes `SELECT pg_advisory_xact_lock(:key)` within the active database transaction.
    The lock is released automatically when the current transaction commits or rolls back.

    Args:
        db: Active SQLAlchemy AsyncSession.
        namespace: Category / domain of the lock target.
        identifier: Identifier of the target resource.

    Returns:
        int: The acquired 64-bit advisory lock key.
    """
    key = derive_advisory_lock_key(namespace, identifier)
    logger.debug(
        "Acquiring PostgreSQL transaction advisory lock for %s:%s (key=%d)",
        namespace,
        identifier,
        key,
    )
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
    return key


async def try_acquire_advisory_lock(
    db: AsyncSession,
    namespace: str,
    identifier: str | uuid.UUID,
) -> bool:
    """Attempt to acquire a transaction-scoped exclusive advisory lock without blocking.

    Executes `SELECT pg_try_advisory_xact_lock(:key)`.

    Returns:
        bool: True if lock was acquired, False if held by another transaction.
    """
    key = derive_advisory_lock_key(namespace, identifier)
    res = await db.execute(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": key})
    return bool(res.scalar())


async def acquire_multi_entity_advisory_locks(
    db: AsyncSession,
    locks: list[tuple[str, str | uuid.UUID]],
) -> list[int]:
    """Acquire transaction-scoped exclusive advisory locks across multiple entities.

    Guarantees:
    1. Empty Input: If `locks` is empty, returns `[]` immediately without DB execution.
    2. Input Validation: Validates that each element is a (namespace, identifier) pair.
    3. Logical Deduplication: Identical (namespace, identifier) pairs in the input are
       consolidated without error or warning.
    4. Collision Detection: If two distinct (namespace, identifier) pairs derive the
       exact same 64-bit integer key, a structured warning is logged.
    5. Monotonic Ascending Order: Unique integer keys are sorted in strictly ascending
       signed integer order before calling `SELECT pg_advisory_xact_lock(:key)`.
    6. Returns the list of acquired keys in sorted order.

    Deadlock Consideration:
    Sorting by derived signed integer eliminates circular-wait cycles among entities
    locked within this level, assuming all concurrent transactions follow the same total
    order and do not interleave acquisitions with higher-level row locks out of hierarchy.

    Args:
        db: Active SQLAlchemy AsyncSession.
        locks: List of (namespace, identifier) pairs to acquire.

    Returns:
        list[int]: Sorted list of acquired 64-bit advisory lock keys.
    """
    if not locks:
        return []

    # 1. Validation & Logical Deduplication
    unique_entities: dict[tuple[str, str], tuple[str, str | uuid.UUID]] = {}
    for item in locks:
        if not isinstance(item, (tuple, list)) or len(item) != 2:
            raise ValueError(f"Each lock item must be a (namespace, identifier) tuple, got {item!r}")
        ns, ident = item
        if not ns or not isinstance(ns, str) or not ns.strip():
            raise ValueError(f"Lock namespace must be a non-empty string, got {ns!r}")
        if ident is None:
            raise ValueError(f"Lock identifier must not be None for namespace '{ns}'")

        clean_ns = ns.strip().lower()

        # Normalize logical identity key for deduplication
        if clean_ns in UUID_NAMESPACES:
            if isinstance(ident, uuid.UUID):
                norm_ident = str(ident).lower()
            elif isinstance(ident, str) and ident.strip():
                try:
                    norm_ident = str(uuid.UUID(ident.strip())).lower()
                except ValueError as err:
                    raise ValueError(f"Invalid UUID '{ident}' for namespace '{clean_ns}': {err}") from err
            else:
                raise ValueError(f"Invalid UUID identifier '{ident}' for namespace '{clean_ns}'")
        elif clean_ns == "stripe_invoice":
            if not isinstance(ident, str) or not ident:
                raise ValueError(f"Identifier must be a non-empty string for stripe_invoice, got {ident!r}")
            norm_ident = ident
        else:
            if isinstance(ident, str) and ident.strip():
                norm_ident = ident.strip()
            elif isinstance(ident, uuid.UUID):
                norm_ident = str(ident).lower()
            else:
                raise ValueError(f"Identifier must be a non-empty string for namespace '{clean_ns}', got {ident!r}")

        logical_key = (clean_ns, norm_ident)
        if logical_key not in unique_entities:
            unique_entities[logical_key] = (ns, ident)

    # 2. Derive 64-bit keys and detect hash collisions across distinct entities
    key_to_entities: dict[int, list[tuple[str, str]]] = {}
    for logical_key, (ns, ident) in unique_entities.items():
        k = derive_advisory_lock_key(ns, ident)
        key_to_entities.setdefault(k, []).append(logical_key)

    # Log structured warning on 64-bit hash collision
    for k, entities in key_to_entities.items():
        if len(entities) > 1:
            logger.warning(
                "Advisory lock 64-bit hash collision detected: distinct entities %s both derived key %d",
                entities,
                k,
            )

    # 3. Sort keys in strictly ascending signed integer order
    sorted_keys = sorted(key_to_entities.keys())

    # 4. Acquire locks in deterministic ascending order
    for k in sorted_keys:
        logger.debug("Acquiring PostgreSQL transaction advisory lock (key=%d)", k)
        await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": k})

    return sorted_keys
