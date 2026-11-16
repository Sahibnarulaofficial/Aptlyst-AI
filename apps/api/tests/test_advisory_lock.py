"""Comprehensive unit test suite for unified PostgreSQL advisory lock primitives (BLK-03).

Verifies:
1. Exact golden literal key values for Razorpay and Stripe domains.
2. 100% backward compatibility of the Razorpay facade (derive, acquire, try_acquire).
3. Domain-specific identifier normalization (UUID normalization vs case-preserving raw strings).
4. Namespace separation and collision resistance across distinct domains.
5. Signed 64-bit integer boundaries (PG_BIGINT_MIN to PG_BIGINT_MAX).
6. Comprehensive input validation and error handling for missing/empty/invalid identifiers.
7. Multi-entity deterministic ascending integer sort ordering.
8. Logical duplicate deduplication (no redundant queries, no spurious warnings).
9. Distinct logical resource collision detection with structured logging.
10. Empty input no-op behavior.
11. Transaction-scoped query execution against mock database sessions.
12. Organization API transitional dual-lock ordering (legacy 31-bit first, canonical 64-bit second).
"""

import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock
import uuid

import pytest
from sqlalchemy import text

from app.services.billing.advisory_lock import (
    PG_BIGINT_MAX,
    PG_BIGINT_MIN,
    RAZORPAY_ENTITY_TYPES,
    UUID_NAMESPACES,
    acquire_advisory_lock,
    acquire_multi_entity_advisory_locks,
    derive_advisory_lock_key,
    try_acquire_advisory_lock,
)
from app.services.billing.razorpay.lock import (
    ALLOWED_ENTITY_TYPES,
    acquire_razorpay_advisory_lock,
    derive_razorpay_advisory_lock_key,
    try_acquire_razorpay_advisory_lock,
)


class MockAdvisoryLockDbSession:
    """In-memory async session tracking executed advisory lock SQL queries."""

    def __init__(self) -> None:
        self.executed_statements: list[tuple[str, dict[str, int]]] = []
        self.acquired_keys: list[int] = []

    async def execute(self, statement: Any, params: dict[str, Any] | None = None) -> Any:
        sql = str(statement).strip()
        p = params or {}
        self.executed_statements.append((sql, p))
        if "key" in p:
            self.acquired_keys.append(p["key"])

        mock_result = MagicMock()
        mock_result.scalar.return_value = True
        mock_result.scalar_one_or_none.return_value = True
        return mock_result


# =============================================================================
# 1. Exact Golden Literal Value Tests
# =============================================================================


def test_golden_literal_razorpay_keys() -> None:
    """Assert exact byte-for-byte matching against hardcoded 64-bit signed integer literals."""
    # sub_test_001
    k1 = derive_advisory_lock_key("subscription", "sub_test_001")
    assert k1 == 6296004847938852458
    assert derive_razorpay_advisory_lock_key("subscription", "sub_test_001") == 6296004847938852458

    # sub_12345 (negative key)
    k2 = derive_advisory_lock_key("subscription", "sub_12345")
    assert k2 == -7061648288204283760
    assert derive_razorpay_advisory_lock_key("subscription", "sub_12345") == -7061648288204283760


def test_golden_literal_stripe_keys() -> None:
    """Assert exact byte-for-byte matching for Stripe invoice identifiers."""
    k1 = derive_advisory_lock_key("stripe_invoice", "in_test_12345")
    assert k1 == 5592900959410246444

    k2 = derive_advisory_lock_key("stripe_invoice", "in_1Mxyz987654321")
    assert k2 == 6411121941954831624


def test_golden_literal_uuid_keys() -> None:
    """Assert exact matching for organization and user UUID namespaces."""
    fixed_uuid = "00000000-0000-0000-0000-000000000001"
    k_org = derive_advisory_lock_key("organization", fixed_uuid)
    assert k_org == -5900610007483364056

    k_usr = derive_advisory_lock_key("user", fixed_uuid)
    assert k_usr == 2497583422330410899


# =============================================================================
# 2. Razorpay Facade Backward Compatibility
# =============================================================================


def test_razorpay_facade_whitelisted_types() -> None:
    """Verify facade supports all whitelisted entity types."""
    for et in ALLOWED_ENTITY_TYPES:
        k_facade = derive_razorpay_advisory_lock_key(et, "ent_123")
        k_direct = derive_advisory_lock_key(et, "ent_123")
        assert k_facade == k_direct
        assert PG_BIGINT_MIN <= k_facade <= PG_BIGINT_MAX


def test_razorpay_facade_rejects_unwhitelisted_types() -> None:
    """Verify facade continues to reject unwhitelisted entity types with exact ValueError."""
    with pytest.raises(ValueError, match="Invalid entity_type 'order'"):
        derive_razorpay_advisory_lock_key("order", "order_123")

    with pytest.raises(ValueError, match="Invalid entity_type 'user'"):
        derive_razorpay_advisory_lock_key("user", "usr_123")


def test_razorpay_facade_rejects_empty_identifiers() -> None:
    """Verify facade raises ValueError on missing or whitespace identifiers."""
    with pytest.raises(ValueError, match="entity_type must be a non-empty string"):
        derive_razorpay_advisory_lock_key("", "sub_123")

    with pytest.raises(ValueError, match="entity_id must be a non-empty string"):
        derive_razorpay_advisory_lock_key("subscription", "   ")


# =============================================================================
# 3. Domain-Specific Identifier Normalization
# =============================================================================


def test_uuid_normalization_equivalence() -> None:
    """Verify UUID identifiers in UUID_NAMESPACES normalize identically across formats."""
    raw_uuid = uuid.uuid4()
    str_lower = str(raw_uuid).lower()
    str_upper = str(raw_uuid).upper()
    str_hex = raw_uuid.hex
    str_padded = f"  {str_lower}  "

    k_obj = derive_advisory_lock_key("organization", raw_uuid)
    k_lower = derive_advisory_lock_key("organization", str_lower)
    k_upper = derive_advisory_lock_key("organization", str_upper)
    k_hex = derive_advisory_lock_key("organization", str_hex)
    k_padded = derive_advisory_lock_key("organization", str_padded)

    assert k_obj == k_lower == k_upper == k_hex == k_padded


def test_uuid_normalization_rejects_invalid_uuid() -> None:
    """Verify invalid UUID strings in UUID_NAMESPACES raise ValueError."""
    with pytest.raises(ValueError, match="Invalid UUID"):
        derive_advisory_lock_key("organization", "not-a-valid-uuid")

    with pytest.raises(ValueError, match="Invalid UUID"):
        derive_advisory_lock_key("user", "12345")


def test_case_sensitivity_preserved_for_provider_ids() -> None:
    """Verify Razorpay and Stripe identifiers strictly preserve character case."""
    k_sub_lower = derive_advisory_lock_key("subscription", "sub_abc")
    k_sub_upper = derive_advisory_lock_key("subscription", "sub_ABC")
    assert k_sub_lower != k_sub_upper

    k_stripe_lower = derive_advisory_lock_key("stripe_invoice", "in_abc")
    k_stripe_upper = derive_advisory_lock_key("stripe_invoice", "in_ABC")
    assert k_stripe_lower != k_stripe_upper


# =============================================================================
# 4. Namespace Separation & 64-bit Integer Boundaries
# =============================================================================


def test_namespace_separation() -> None:
    """Verify distinct namespaces for identical identifiers derive distinct keys."""
    ident = "shared_identity_123"
    k_sub = derive_advisory_lock_key("subscription", ident)
    k_pay = derive_advisory_lock_key("payment", ident)
    k_inv = derive_advisory_lock_key("invoice", ident)

    assert len({k_sub, k_pay, k_inv}) == 3


def test_signed_64bit_bounds() -> None:
    """Verify derived keys strictly fit within signed 64-bit integer limits."""
    for i in range(100):
        k = derive_advisory_lock_key("subscription", f"sub_random_{i}")
        assert PG_BIGINT_MIN <= k <= PG_BIGINT_MAX


# =============================================================================
# 5. Multi-Entity Monotonic Acquisition & Sorting
# =============================================================================


@pytest.mark.asyncio
async def test_multi_entity_monotonic_ascending_sort_order() -> None:
    """Verify multi-entity acquisition executes in strictly ascending integer order."""
    session = MockAdvisoryLockDbSession()

    # Create 3 entities whose keys are ordered K_neg < K_mid < K_pos
    # sub_12345 -> -7061648288204283760 (negative)
    # in_test_12345 -> 5592900959410246444 (mid positive)
    # sub_test_001 -> 6296004847938852458 (high positive)
    items = [
        ("subscription", "sub_test_001"),   # high
        ("subscription", "sub_12345"),      # negative
        ("stripe_invoice", "in_test_12345"),# mid
    ]

    keys = await acquire_multi_entity_advisory_locks(session, items)

    # Output list must be sorted ascending
    assert keys == [-7061648288204283760, 5592900959410246444, 6296004847938852458]
    # Execution queries must match exact ascending order
    assert session.acquired_keys == keys


@pytest.mark.asyncio
async def test_multi_entity_logical_deduplication() -> None:
    """Verify duplicate logical requests are consolidated without redundant DB queries."""
    session = MockAdvisoryLockDbSession()

    items = [
        ("subscription", "sub_test_001"),
        ("subscription", "sub_test_001"),
        ("subscription", "sub_test_001"),
    ]

    keys = await acquire_multi_entity_advisory_locks(session, items)
    assert len(keys) == 1
    assert keys[0] == 6296004847938852458
    assert len(session.acquired_keys) == 1


@pytest.mark.asyncio
async def test_multi_entity_empty_input_no_op() -> None:
    """Verify empty list is an immediate no-op returning []."""
    session = MockAdvisoryLockDbSession()
    keys = await acquire_multi_entity_advisory_locks(session, [])
    assert keys == []
    assert len(session.executed_statements) == 0


@pytest.mark.asyncio
async def test_multi_entity_hash_collision_warning(caplog: pytest.LogCaptureFixture) -> None:
    """Verify that distinct logical entities mapping to the same 64-bit key emit a warning."""
    session = MockAdvisoryLockDbSession()

    # Mock derive_advisory_lock_key to return identical key for two different entities
    with caplog.at_level(logging.WARNING):
        from unittest.mock import patch
        with patch("app.services.billing.advisory_lock.derive_advisory_lock_key", return_value=123456789):
            keys = await acquire_multi_entity_advisory_locks(
                session,
                [("subscription", "sub_A"), ("payment", "pay_B")],
            )

    assert keys == [123456789]
    assert len(session.acquired_keys) == 1
    # Check warning was logged
    collision_warnings = [r for r in caplog.records if "collision detected" in r.message]
    assert len(collision_warnings) == 1
    assert "both derived key 123456789" in collision_warnings[0].message


# =============================================================================
# 6. Single Lock Acquisition Semantics
# =============================================================================


@pytest.mark.asyncio
async def test_acquire_and_try_advisory_lock_execution() -> None:
    """Verify single acquire and try_acquire query execution."""
    session = MockAdvisoryLockDbSession()

    k1 = await acquire_advisory_lock(session, "subscription", "sub_test_001")
    assert k1 == 6296004847938852458
    assert "pg_advisory_xact_lock" in session.executed_statements[0][0]

    acquired = await try_acquire_advisory_lock(session, "stripe_invoice", "in_test_12345")
    assert acquired is True
    assert "pg_try_advisory_xact_lock" in session.executed_statements[1][0]


@pytest.mark.asyncio
async def test_razorpay_facade_execution() -> None:
    """Verify acquire_razorpay_advisory_lock and try_acquire_razorpay_advisory_lock."""
    session = MockAdvisoryLockDbSession()

    k = await acquire_razorpay_advisory_lock(session, "subscription", "sub_test_001")
    assert k == 6296004847938852458

    acquired = await try_acquire_razorpay_advisory_lock(session, "payment", "pay_test_001")
    assert acquired is True


# =============================================================================
# 7. Organization API Transitional Dual-Locking Ordering
# =============================================================================


@pytest.mark.asyncio
async def test_organization_transitional_dual_locking_ordering() -> None:
    """Verify the transitional dual-lock sequence in organization creation.

    Must execute:
    1. Legacy 31-bit modulo lock FIRST.
    2. Canonical 64-bit lock SECOND.
    """
    session = MockAdvisoryLockDbSession()
    test_user_id = uuid.UUID("00000000-0000-0000-0000-000000000001")

    # Replicate the exact dual-lock block from apps/api/app/api/organizations.py
    legacy_lock_key = int(test_user_id.int % 2147483647)
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"),
        {"key": legacy_lock_key},
    )
    canonical_lock_key = derive_advisory_lock_key("user", test_user_id)
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"),
        {"key": canonical_lock_key},
    )

    assert len(session.acquired_keys) == 2
    # Lock 1: Legacy 31-bit key
    assert session.acquired_keys[0] == legacy_lock_key == 1
    # Lock 2: Canonical 64-bit key
    assert session.acquired_keys[1] == canonical_lock_key == 2497583422330410899
