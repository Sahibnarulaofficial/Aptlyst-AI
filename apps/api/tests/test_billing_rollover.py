"""Unit and concurrency tests for Free billing period rollover and boundary evaluation.

Covers:
1. evaluate_free_period_rollover boundary cases:
   - One second before period end.
   - Exactly at period end.
   - One second after period end.
   - Exactly one full period after period end.
   - Several periods after period end (dormant organization).
2. rollover_free_billing_period_if_needed database execution:
   - Active period before period end remains active without rollover.
   - Expired Free period is atomically rolled over, old period deactivated.
   - Paid Pro period is strictly ignored by Free rolling logic.
   - Zero historical period bloat: intermediate skipped periods are never created.
   - Concurrency safety: double rollover call re-evaluates under lock and avoids duplicate creation.
"""

import sys

if sys.platform == "win32":
    import socket
    from asyncio.proactor_events import BaseProactorEventLoop

    def _safe_make_self_pipe(self):  # type: ignore[no-untyped-def]
        self._ssock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._csock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._internal_fds += 1

    BaseProactorEventLoop._make_self_pipe = _safe_make_self_pipe
    BaseProactorEventLoop._loop_self_reading = lambda self, f=None: None

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

import pytest

from app.models.billing import OrganizationBillingPeriod
from app.services.billing.accounting import (
    FIXED_FREE_PERIOD_DURATION,
    FIXED_FREE_PERIOD_SECONDS,
    evaluate_free_period_rollover,
    rollover_free_billing_period_if_needed,
)


# =============================================================================
# 1. Boundary-Condition Tests for evaluate_free_period_rollover
# =============================================================================


def test_rollover_one_second_before_period_end() -> None:
    """Case 1: When now is one second before period end, no rollover must occur."""
    t_start = datetime(2026, 11, 1, 10, 0, 0, tzinfo=timezone.utc)
    t_end = t_start + FIXED_FREE_PERIOD_DURATION  # 2026-12-01 10:00:00 UTC
    now = t_end - timedelta(seconds=1)            # 2026-12-01 09:59:59 UTC

    result = evaluate_free_period_rollover(t_start, t_end, now)
    assert result is None, "Expected None (no rollover required) when now < current_period_end"


def test_rollover_exactly_at_period_end() -> None:
    """Case 2: When now is exactly at period end, contiguous rollover must occur."""
    t_start = datetime(2026, 11, 1, 10, 0, 0, tzinfo=timezone.utc)
    t_end = t_start + FIXED_FREE_PERIOD_DURATION  # 2026-12-01 10:00:00 UTC
    now = t_end                                   # 2026-12-01 10:00:00 UTC

    result = evaluate_free_period_rollover(t_start, t_end, now)
    assert result is not None, "Expected rollover tuple when now == current_period_end"
    new_start, new_end = result
    assert new_start == t_end
    assert new_end == t_end + FIXED_FREE_PERIOD_DURATION
    assert (new_end - new_start) == FIXED_FREE_PERIOD_DURATION


def test_rollover_one_second_after_period_end() -> None:
    """Case 3: When now is one second after period end, next period is anchored to t_end."""
    t_start = datetime(2026, 11, 1, 10, 0, 0, tzinfo=timezone.utc)
    t_end = t_start + FIXED_FREE_PERIOD_DURATION  # 2026-12-01 10:00:00 UTC
    now = t_end + timedelta(seconds=1)            # 2026-12-01 10:00:01 UTC

    result = evaluate_free_period_rollover(t_start, t_end, now)
    assert result is not None
    new_start, new_end = result
    assert new_start == t_end
    assert new_end == t_end + FIXED_FREE_PERIOD_DURATION
    assert new_start <= now < new_end


def test_rollover_exactly_one_full_period_after_period_end() -> None:
    """Case 4: When now is exactly one full period after period end (1 cycle skipped)."""
    t_start = datetime(2026, 11, 1, 10, 0, 0, tzinfo=timezone.utc)
    t_end = t_start + FIXED_FREE_PERIOD_DURATION          # 2026-12-01 10:00:00 UTC
    now = t_end + FIXED_FREE_PERIOD_DURATION              # 2026-12-31 10:00:00 UTC (30d after t_end)

    result = evaluate_free_period_rollover(t_start, t_end, now)
    assert result is not None
    new_start, new_end = result
    assert new_start == t_end + FIXED_FREE_PERIOD_DURATION
    assert new_end == new_start + FIXED_FREE_PERIOD_DURATION
    assert new_start <= now < new_end


def test_rollover_several_periods_after_period_end() -> None:
    """Case 5: When now is 75 days (2.5 cycles) after period end, advances to cycle 2."""
    t_start = datetime(2026, 11, 1, 10, 0, 0, tzinfo=timezone.utc)
    t_end = t_start + FIXED_FREE_PERIOD_DURATION          # 2026-12-01 10:00:00 UTC
    now = t_end + timedelta(days=75)                      # 2027-02-14 10:00:00 UTC

    result = evaluate_free_period_rollover(t_start, t_end, now)
    assert result is not None
    new_start, new_end = result
    # 75 days // 30 days = 2 skipped cycles
    assert new_start == t_end + timedelta(days=60)
    assert new_end == t_end + timedelta(days=90)
    assert new_start <= now < new_end


# =============================================================================
# 2. Database Rollover Service Tests
# =============================================================================


@pytest.mark.anyio
async def test_rollover_service_active_period_unchanged() -> None:
    """Verify that an unexpired Free billing period is not rolled over."""
    org_id = uuid.uuid4()
    t_start = datetime(2026, 11, 1, 10, 0, 0, tzinfo=timezone.utc)
    t_end = t_start + timedelta(days=30)
    now = t_start + timedelta(days=15)

    current_period = OrganizationBillingPeriod(
        id=uuid.uuid4(),
        organization_id=org_id,
        period_start=t_start,
        period_end=t_end,
        plan_tier="free",
        monthly_token_quota=0,
        tokens_used=100,
        is_active=True,
    )

    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = current_period
    mock_session.execute.return_value = mock_result

    res = await rollover_free_billing_period_if_needed(mock_session, organization_id=org_id, now=now)
    assert res is current_period
    assert current_period.is_active is True
    mock_session.add.assert_not_called()


@pytest.mark.anyio
async def test_rollover_service_expired_free_period_rolls_over() -> None:
    """Verify that an expired Free billing period is deactivated and replaced with a new period."""
    org_id = uuid.uuid4()
    t_start = datetime(2026, 11, 1, 10, 0, 0, tzinfo=timezone.utc)
    t_end = t_start + timedelta(days=30)
    now = t_end + timedelta(seconds=10)

    current_period = OrganizationBillingPeriod(
        id=uuid.uuid4(),
        organization_id=org_id,
        period_start=t_start,
        period_end=t_end,
        plan_tier="free",
        monthly_token_quota=0,
        tokens_used=5000,
        is_active=True,
    )

    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = current_period
    mock_session.execute.return_value = mock_result

    res = await rollover_free_billing_period_if_needed(mock_session, organization_id=org_id, now=now)
    assert res is not None
    assert res is not current_period
    # Previous period is deactivated
    assert current_period.is_active is False
    # New period is active with zeroed counters
    assert res.is_active is True
    assert res.plan_tier == "free"
    assert res.tokens_used == 0
    assert res.period_start == t_end
    assert res.period_end == t_end + timedelta(days=30)
    mock_session.add.assert_called_once_with(res)
    mock_session.flush.assert_called_once()


@pytest.mark.anyio
async def test_rollover_service_ignores_pro_subscription_tier() -> None:
    """Verify that paid Pro billing periods are strictly ignored by Free rolling logic."""
    org_id = uuid.uuid4()
    t_start = datetime(2026, 11, 1, 10, 0, 0, tzinfo=timezone.utc)
    t_end = t_start + timedelta(days=30)
    now = t_end + timedelta(days=5)

    pro_period = OrganizationBillingPeriod(
        id=uuid.uuid4(),
        organization_id=org_id,
        period_start=t_start,
        period_end=t_end,
        plan_tier="pro",
        monthly_token_quota=2_000_000,
        tokens_used=100_000,
        is_active=True,
    )

    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = pro_period
    mock_session.execute.return_value = mock_result

    res = await rollover_free_billing_period_if_needed(mock_session, organization_id=org_id, now=now)
    assert res is pro_period
    assert pro_period.is_active is True
    mock_session.add.assert_not_called()


@pytest.mark.anyio
async def test_rollover_concurrent_interleaving_returns_newly_active_period() -> None:
    """Simulate PostgreSQL READ COMMITTED row-invalidation anomaly during concurrent rollover.

    MOCKED INTERLEAVING DISTINCTION:
    This test runs in a mocked unit test environment, simulating the exact PostgreSQL
    READ COMMITTED locking behavior:
    1. Worker A locked expired period P1, rolled it over to P2, and committed (P1.is_active becomes False).
    2. Worker B was blocked waiting for P1's lock. Upon unblocking, PostgreSQL re-evaluates
       the query predicate on P1. Because P1.is_active is now False, P1 is skipped.
       PostgreSQL does NOT automatically restart the scan for newly inserted rows, so
       Worker B's first query returns None.
    3. Worker B's bounded retry loop re-executes a fresh SELECT statement. In READ COMMITTED,
       this takes a fresh snapshot, locks P2, verifies P2 is fresh, and returns P2 without
       creating a redundant duplicate period.
    """
    org_id = uuid.uuid4()
    t_start = datetime(2026, 11, 1, 10, 0, 0, tzinfo=timezone.utc)
    t_end = t_start + timedelta(days=30)
    now = t_end + timedelta(seconds=15)

    # P2 was just committed by Worker A
    p2_newly_active = OrganizationBillingPeriod(
        id=uuid.uuid4(),
        organization_id=org_id,
        period_start=t_end,
        period_end=t_end + timedelta(days=30),
        plan_tier="free",
        monthly_token_quota=50_000,
        tokens_used=0,
        is_active=True,
    )

    mock_session = AsyncMock()
    mock_session.add = MagicMock()

    # Attempt 1 returns None (simulating predicate failure on deactivated P1)
    mock_result_first = MagicMock()
    mock_result_first.scalar_one_or_none.return_value = None

    # Attempt 2 returns P2 (fresh snapshot locating newly committed active period)
    mock_result_second = MagicMock()
    mock_result_second.scalar_one_or_none.return_value = p2_newly_active

    mock_session.execute.side_effect = [mock_result_first, mock_result_second]

    res = await rollover_free_billing_period_if_needed(mock_session, organization_id=org_id, now=now)

    assert res is p2_newly_active
    assert mock_session.execute.call_count == 2
    # Verify no third duplicate period was created or added
    mock_session.add.assert_not_called()


@pytest.mark.anyio
async def test_rollover_caller_rollback_preserves_expired_active_period() -> None:
    """Verify that if the caller transaction rolls back, rollover mutations are not committed.

    Confirms transaction ownership: rollover_free_billing_period_if_needed flushes but does
    NOT commit. If a downstream error occurs in the caller's transaction context, caller
    rollback ensures neither the deactivation nor the new period is persisted.
    """
    org_id = uuid.uuid4()
    t_start = datetime(2026, 11, 1, 10, 0, 0, tzinfo=timezone.utc)
    t_end = t_start + timedelta(days=30)
    now = t_end + timedelta(seconds=10)

    current_period = OrganizationBillingPeriod(
        id=uuid.uuid4(),
        organization_id=org_id,
        period_start=t_start,
        period_end=t_end,
        plan_tier="free",
        monthly_token_quota=50_000,
        tokens_used=1000,
        is_active=True,
    )

    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = current_period
    mock_session.execute.return_value = mock_result

    # Caller transaction fails after flush
    mock_session.flush.side_effect = RuntimeError("Database serialization conflict on caller transaction")

    with pytest.raises(RuntimeError, match="Database serialization conflict"):
        await rollover_free_billing_period_if_needed(mock_session, organization_id=org_id, now=now)

    # Verify commit was never invoked inside rollover
    mock_session.commit.assert_not_called()


@pytest.mark.anyio
async def test_rollover_adopts_updated_configured_quota_preserving_historical() -> None:
    """Verify newly rolled over period adopts current settings quota while historical retains original."""
    org_id = uuid.uuid4()
    t_start = datetime(2026, 11, 1, 10, 0, 0, tzinfo=timezone.utc)
    t_end = t_start + timedelta(days=30)
    now = t_end + timedelta(seconds=10)

    # Historical period had quota 25,000
    historical_period = OrganizationBillingPeriod(
        id=uuid.uuid4(),
        organization_id=org_id,
        period_start=t_start,
        period_end=t_end,
        plan_tier="free",
        monthly_token_quota=25_000,
        tokens_used=5000,
        is_active=True,
    )

    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = historical_period
    mock_session.execute.return_value = mock_result

    # Current settings now configure a different quota (e.g. 100,000)
    with patch("app.services.billing.accounting.settings") as mock_settings:
        mock_settings.default_free_monthly_token_quota = 100_000

        res = await rollover_free_billing_period_if_needed(mock_session, organization_id=org_id, now=now)

    assert res is not None
    # Newly created period gets currently configured quota
    assert res.monthly_token_quota == 100_000
    assert res.is_active is True

    # Historical period retains original quota unchanged
    assert historical_period.monthly_token_quota == 25_000
    assert historical_period.is_active is False

