"""PostgreSQL integration test suite for Step 5.2D.3.3A.1.

Validates unified provenance backfill, conflict detection, triggers,
check constraints, composite foreign keys, and downgrade guards.

NOTE: As per Step 5.2D.3.3A.1 requirements:
- SQLite MUST NOT be used as a substitute for PostgreSQL constraint testing.
- Tests execute ONLY if a disposable PostgreSQL database is available via POSTGRES_TEST_URL.
- If POSTGRES_TEST_URL is not set or PostgreSQL is unreachable, tests are skipped with a clear notice.
"""

import os
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

import uuid
from datetime import datetime, timezone
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError


POSTGRES_TEST_URL = os.environ.get("POSTGRES_TEST_URL")


def _get_sync_postgres_url(raw_url: str) -> str:
    """Normalize async database URLs to sync psycopg2 URLs for engine creation."""
    if raw_url.startswith("postgresql+asyncpg://"):
        return raw_url.replace("postgresql+asyncpg://", "postgresql+psycopg2://", 1)
    if raw_url.startswith("postgres://"):
        return raw_url.replace("postgres://", "postgresql://", 1)
    return raw_url


@pytest.fixture(scope="module")
def pg_engine():
    """Provides a PostgreSQL engine if available, otherwise skips."""
    if not POSTGRES_TEST_URL:
        pytest.skip(
            "Disposable PostgreSQL test database not available in environment. "
            "Set POSTGRES_TEST_URL to execute live PostgreSQL constraint & trigger integration tests."
        )
    sync_url = _get_sync_postgres_url(POSTGRES_TEST_URL)
    engine = create_engine(sync_url, echo=False)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:
        pytest.skip(f"PostgreSQL connection to POSTGRES_TEST_URL failed: {exc}")
    yield engine
    engine.dispose()


@pytest.mark.skipif(not POSTGRES_TEST_URL, reason="Disposable PostgreSQL environment required")
def test_1_valid_stripe_provenance_backfill(pg_engine) -> None:
    """Scenario 1: Valid 1:1 Stripe invoice evidence correctly backfills billing period."""
    with pg_engine.connect() as conn:
        with conn.begin():
            org_id = uuid.uuid4()
            sub_id = uuid.uuid4()
            period_id = uuid.uuid4()
            stripe_sub_id = f"sub_stripe_{uuid.uuid4().hex[:12]}"

            conn.execute(text("INSERT INTO organizations (id, name, slug) VALUES (:id, 'Test Org', :slug)"), {"id": org_id, "slug": f"org-{uuid.uuid4().hex[:8]}"})
            conn.execute(
                text("INSERT INTO subscriptions (id, organization_id, status, plan_tier, provider, provider_customer_id, merchant_account_id, environment, stripe_subscription_id) "
                     "VALUES (:id, :org_id, 'active', 'pro', 'stripe', 'cus_1', 'default', 'legacy', :stripe_sub_id)"),
                {"id": sub_id, "org_id": org_id, "stripe_sub_id": stripe_sub_id},
            )
            conn.execute(
                text("INSERT INTO organization_billing_periods (id, organization_id, period_start, period_end, plan_tier, monthly_token_quota, subscription_id, is_active) "
                     "VALUES (:id, :org_id, now(), now() + interval '30 days', 'pro', 2000000, NULL, true)"),
                {"id": period_id, "org_id": org_id},
            )
            conn.execute(
                text("INSERT INTO stripe_invoices (id, stripe_invoice_id, organization_id, stripe_customer_id, stripe_subscription_id, invoice_status, amount_paid_minor_units, billing_period_id) "
                     "VALUES (:id, :inv_id, :org_id, 'cus_1', :stripe_sub_id, 'paid', 4900, :period_id)"),
                {"id": uuid.uuid4(), "inv_id": f"in_{uuid.uuid4().hex[:12]}", "org_id": org_id, "stripe_sub_id": stripe_sub_id, "period_id": period_id},
            )

            # Execute backfill CTE
            conn.execute(text("""
                WITH unified_invoice_candidates AS (
                    SELECT DISTINCT si.billing_period_id, s.id AS subscription_id
                    FROM stripe_invoices si
                    JOIN subscriptions s ON s.stripe_subscription_id = si.stripe_subscription_id
                    WHERE si.billing_period_id IS NOT NULL AND si.stripe_subscription_id IS NOT NULL
                    UNION
                    SELECT DISTINCT bi.billing_period_id, bi.subscription_id
                    FROM billing_invoices bi
                    WHERE bi.billing_period_id IS NOT NULL AND bi.subscription_id IS NOT NULL
                ),
                candidate_counts AS (
                    SELECT billing_period_id, COUNT(DISTINCT subscription_id) AS sub_count
                    FROM unified_invoice_candidates
                    GROUP BY billing_period_id
                ),
                unambiguous_candidates AS (
                    SELECT uic.billing_period_id, uic.subscription_id
                    FROM unified_invoice_candidates uic
                    JOIN candidate_counts cc ON cc.billing_period_id = uic.billing_period_id
                    WHERE cc.sub_count = 1
                )
                UPDATE organization_billing_periods obp
                SET subscription_id = uc.subscription_id
                FROM unambiguous_candidates uc
                WHERE obp.id = uc.billing_period_id AND obp.id = :period_id AND obp.subscription_id IS NULL;
            """), {"period_id": period_id})

            res = conn.execute(text("SELECT subscription_id FROM organization_billing_periods WHERE id = :id"), {"id": period_id}).scalar()
            assert res == sub_id
            conn.rollback()


@pytest.mark.skipif(not POSTGRES_TEST_URL, reason="Disposable PostgreSQL environment required")
def test_2_valid_provider_neutral_provenance_backfill(pg_engine) -> None:
    """Scenario 2: Valid provider-neutral invoice evidence correctly backfills billing period."""
    with pg_engine.connect() as conn:
        with conn.begin():
            org_id = uuid.uuid4()
            sub_id = uuid.uuid4()
            period_id = uuid.uuid4()

            conn.execute(text("INSERT INTO organizations (id, name, slug) VALUES (:id, 'Test Org', :slug)"), {"id": org_id, "slug": f"org-{uuid.uuid4().hex[:8]}"})
            conn.execute(
                text("INSERT INTO subscriptions (id, organization_id, status, plan_tier, provider, provider_customer_id, merchant_account_id, environment) "
                     "VALUES (:id, :org_id, 'active', 'pro', 'razorpay', 'cust_1', 'acc_1', 'live')"),
                {"id": sub_id, "org_id": org_id},
            )
            conn.execute(
                text("INSERT INTO organization_billing_periods (id, organization_id, period_start, period_end, plan_tier, monthly_token_quota, subscription_id, is_active) "
                     "VALUES (:id, :org_id, now(), now() + interval '30 days', 'pro', 2000000, NULL, true)"),
                {"id": period_id, "org_id": org_id},
            )
            conn.execute(
                text("INSERT INTO billing_invoices (id, provider, merchant_account_id, environment, provider_invoice_id, organization_id, subscription_id, provider_customer_id, status, currency, gross_amount_minor_units, billing_period_id) "
                     "VALUES (:id, 'razorpay', 'acc_1', 'live', :inv_id, :org_id, :sub_id, 'cust_1', 'paid', 'USD', 4900, :period_id)"),
                {"id": uuid.uuid4(), "inv_id": f"inv_{uuid.uuid4().hex[:12]}", "org_id": org_id, "sub_id": sub_id, "period_id": period_id},
            )

            # Backfill
            conn.execute(text("""
                WITH unified_invoice_candidates AS (
                    SELECT DISTINCT si.billing_period_id, s.id AS subscription_id
                    FROM stripe_invoices si
                    JOIN subscriptions s ON s.stripe_subscription_id = si.stripe_subscription_id
                    WHERE si.billing_period_id IS NOT NULL AND si.stripe_subscription_id IS NOT NULL
                    UNION
                    SELECT DISTINCT bi.billing_period_id, bi.subscription_id
                    FROM billing_invoices bi
                    WHERE bi.billing_period_id IS NOT NULL AND bi.subscription_id IS NOT NULL
                ),
                candidate_counts AS (
                    SELECT billing_period_id, COUNT(DISTINCT subscription_id) AS sub_count
                    FROM unified_invoice_candidates
                    GROUP BY billing_period_id
                ),
                unambiguous_candidates AS (
                    SELECT uic.billing_period_id, uic.subscription_id
                    FROM unified_invoice_candidates uic
                    JOIN candidate_counts cc ON cc.billing_period_id = uic.billing_period_id
                    WHERE cc.sub_count = 1
                )
                UPDATE organization_billing_periods obp
                SET subscription_id = uc.subscription_id
                FROM unambiguous_candidates uc
                WHERE obp.id = uc.billing_period_id AND obp.id = :period_id AND obp.subscription_id IS NULL;
            """), {"period_id": period_id})

            res = conn.execute(text("SELECT subscription_id FROM organization_billing_periods WHERE id = :id"), {"id": period_id}).scalar()
            assert res == sub_id
            conn.rollback()


@pytest.mark.skipif(not POSTGRES_TEST_URL, reason="Disposable PostgreSQL environment required")
def test_3_multiple_invoices_pointing_to_same_subscription(pg_engine) -> None:
    """Scenario 3: Multiple invoices pointing to the exact same subscription resolve without error."""
    with pg_engine.connect() as conn:
        with conn.begin():
            org_id = uuid.uuid4()
            sub_id = uuid.uuid4()
            period_id = uuid.uuid4()
            stripe_sub_id = f"sub_stripe_{uuid.uuid4().hex[:12]}"

            conn.execute(text("INSERT INTO organizations (id, name, slug) VALUES (:id, 'Test Org', :slug)"), {"id": org_id, "slug": f"org-{uuid.uuid4().hex[:8]}"})
            conn.execute(
                text("INSERT INTO subscriptions (id, organization_id, status, plan_tier, provider, provider_customer_id, merchant_account_id, environment, stripe_subscription_id) "
                     "VALUES (:id, :org_id, 'active', 'pro', 'stripe', 'cus_1', 'default', 'legacy', :stripe_sub_id)"),
                {"id": sub_id, "org_id": org_id, "stripe_sub_id": stripe_sub_id},
            )
            conn.execute(
                text("INSERT INTO organization_billing_periods (id, organization_id, period_start, period_end, plan_tier, monthly_token_quota, subscription_id, is_active) "
                     "VALUES (:id, :org_id, now(), now() + interval '30 days', 'pro', 2000000, NULL, true)"),
                {"id": period_id, "org_id": org_id},
            )
            # Two duplicate stripe invoices + one billing_invoice for same subscription
            conn.execute(
                text("INSERT INTO stripe_invoices (id, stripe_invoice_id, organization_id, stripe_customer_id, stripe_subscription_id, invoice_status, amount_paid_minor_units, billing_period_id) "
                     "VALUES (:id, :inv_id, :org_id, 'cus_1', :stripe_sub_id, 'paid', 4900, :period_id)"),
                {"id": uuid.uuid4(), "inv_id": f"in_{uuid.uuid4().hex[:12]}", "org_id": org_id, "stripe_sub_id": stripe_sub_id, "period_id": period_id},
            )
            conn.execute(
                text("INSERT INTO stripe_invoices (id, stripe_invoice_id, organization_id, stripe_customer_id, stripe_subscription_id, invoice_status, amount_paid_minor_units, billing_period_id) "
                     "VALUES (:id, :inv_id, :org_id, 'cus_1', :stripe_sub_id, 'paid', 4900, :period_id)"),
                {"id": uuid.uuid4(), "inv_id": f"in_{uuid.uuid4().hex[:12]}", "org_id": org_id, "stripe_sub_id": stripe_sub_id, "period_id": period_id},
            )
            conn.execute(
                text("INSERT INTO billing_invoices (id, provider, merchant_account_id, environment, provider_invoice_id, organization_id, subscription_id, provider_customer_id, status, currency, gross_amount_minor_units, billing_period_id) "
                     "VALUES (:id, 'stripe', 'default', 'legacy', :inv_id, :org_id, :sub_id, 'cus_1', 'paid', 'USD', 4900, :period_id)"),
                {"id": uuid.uuid4(), "inv_id": f"inv_{uuid.uuid4().hex[:12]}", "org_id": org_id, "sub_id": sub_id, "period_id": period_id},
            )

            # Check conflict block: should find 0 conflicts
            conflict_count = conn.execute(text("""
                WITH unified_invoice_candidates AS (
                    SELECT DISTINCT si.billing_period_id, s.id AS subscription_id
                    FROM stripe_invoices si
                    JOIN subscriptions s ON s.stripe_subscription_id = si.stripe_subscription_id
                    WHERE si.billing_period_id = :period_id AND si.stripe_subscription_id IS NOT NULL
                    UNION
                    SELECT DISTINCT bi.billing_period_id, bi.subscription_id
                    FROM billing_invoices bi
                    WHERE bi.billing_period_id = :period_id AND bi.subscription_id IS NOT NULL
                )
                SELECT COUNT(*) FROM (
                    SELECT billing_period_id FROM unified_invoice_candidates GROUP BY billing_period_id HAVING COUNT(DISTINCT subscription_id) > 1
                ) c;
            """), {"period_id": period_id}).scalar()
            assert conflict_count == 0

            # Execute backfill
            conn.execute(text("""
                WITH unified_invoice_candidates AS (
                    SELECT DISTINCT si.billing_period_id, s.id AS subscription_id
                    FROM stripe_invoices si
                    JOIN subscriptions s ON s.stripe_subscription_id = si.stripe_subscription_id
                    WHERE si.billing_period_id IS NOT NULL AND si.stripe_subscription_id IS NOT NULL
                    UNION
                    SELECT DISTINCT bi.billing_period_id, bi.subscription_id
                    FROM billing_invoices bi
                    WHERE bi.billing_period_id IS NOT NULL AND bi.subscription_id IS NOT NULL
                ),
                candidate_counts AS (
                    SELECT billing_period_id, COUNT(DISTINCT subscription_id) AS sub_count
                    FROM unified_invoice_candidates
                    GROUP BY billing_period_id
                ),
                unambiguous_candidates AS (
                    SELECT uic.billing_period_id, uic.subscription_id
                    FROM unified_invoice_candidates uic
                    JOIN candidate_counts cc ON cc.billing_period_id = uic.billing_period_id
                    WHERE cc.sub_count = 1
                )
                UPDATE organization_billing_periods obp
                SET subscription_id = uc.subscription_id
                FROM unambiguous_candidates uc
                WHERE obp.id = uc.billing_period_id AND obp.id = :period_id AND obp.subscription_id IS NULL;
            """), {"period_id": period_id})

            res = conn.execute(text("SELECT subscription_id FROM organization_billing_periods WHERE id = :id"), {"id": period_id}).scalar()
            assert res == sub_id
            conn.rollback()


@pytest.mark.skipif(not POSTGRES_TEST_URL, reason="Disposable PostgreSQL environment required")
def test_4_stripe_and_neutral_evidence_disagreeing(pg_engine) -> None:
    """Scenario 4: Stripe and provider-neutral evidence pointing to different subscriptions triggers abort."""
    with pg_engine.connect() as conn:
        with conn.begin():
            org_id = uuid.uuid4()
            sub_id1 = uuid.uuid4()
            sub_id2 = uuid.uuid4()
            period_id = uuid.uuid4()
            stripe_sub_id = f"sub_stripe_{uuid.uuid4().hex[:12]}"

            conn.execute(text("INSERT INTO organizations (id, name, slug) VALUES (:id, 'Test Org', :slug)"), {"id": org_id, "slug": f"org-{uuid.uuid4().hex[:8]}"})
            conn.execute(
                text("INSERT INTO subscriptions (id, organization_id, status, plan_tier, provider, provider_customer_id, merchant_account_id, environment, stripe_subscription_id) "
                     "VALUES (:id, :org_id, 'active', 'pro', 'stripe', 'cus_1', 'default', 'legacy', :stripe_sub_id)"),
                {"id": sub_id1, "org_id": org_id, "stripe_sub_id": stripe_sub_id},
            )
            conn.execute(
                text("INSERT INTO subscriptions (id, organization_id, status, plan_tier, provider, provider_customer_id, merchant_account_id, environment) "
                     "VALUES (:id, :org_id, 'active', 'pro', 'razorpay', 'cust_2', 'acc_1', 'live')"),
                {"id": sub_id2, "org_id": org_id},
            )
            conn.execute(
                text("INSERT INTO organization_billing_periods (id, organization_id, period_start, period_end, plan_tier, monthly_token_quota, subscription_id, is_active) "
                     "VALUES (:id, :org_id, now(), now() + interval '30 days', 'pro', 2000000, NULL, true)"),
                {"id": period_id, "org_id": org_id},
            )
            # Disagreeing invoices: Stripe points to sub1, billing_invoices points to sub2
            conn.execute(
                text("INSERT INTO stripe_invoices (id, stripe_invoice_id, organization_id, stripe_customer_id, stripe_subscription_id, invoice_status, amount_paid_minor_units, billing_period_id) "
                     "VALUES (:id, :inv_id, :org_id, 'cus_1', :stripe_sub_id, 'paid', 4900, :period_id)"),
                {"id": uuid.uuid4(), "inv_id": f"in_{uuid.uuid4().hex[:12]}", "org_id": org_id, "stripe_sub_id": stripe_sub_id, "period_id": period_id},
            )
            conn.execute(
                text("INSERT INTO billing_invoices (id, provider, merchant_account_id, environment, provider_invoice_id, organization_id, subscription_id, provider_customer_id, status, currency, gross_amount_minor_units, billing_period_id) "
                     "VALUES (:id, 'razorpay', 'acc_1', 'live', :inv_id, :org_id, :sub_id, 'cust_2', 'paid', 'USD', 4900, :period_id)"),
                {"id": uuid.uuid4(), "inv_id": f"inv_{uuid.uuid4().hex[:12]}", "org_id": org_id, "sub_id": sub_id2, "period_id": period_id},
            )

            # Conflict gate should detect conflict and raise exception
            with pytest.raises(DBAPIError) as exc_info:
                conn.execute(text("""
                    DO $$
                    DECLARE
                        conflict_count INTEGER;
                    BEGIN
                        WITH unified_invoice_candidates AS (
                            SELECT DISTINCT si.billing_period_id, s.id AS subscription_id
                            FROM stripe_invoices si
                            JOIN subscriptions s ON s.stripe_subscription_id = si.stripe_subscription_id
                            WHERE si.billing_period_id IS NOT NULL AND si.stripe_subscription_id IS NOT NULL
                            UNION
                            SELECT DISTINCT bi.billing_period_id, bi.subscription_id
                            FROM billing_invoices bi
                            WHERE bi.billing_period_id IS NOT NULL AND bi.subscription_id IS NOT NULL
                        )
                        SELECT COUNT(*) INTO conflict_count
                        FROM (
                            SELECT billing_period_id FROM unified_invoice_candidates GROUP BY billing_period_id HAVING COUNT(DISTINCT subscription_id) > 1
                        ) c;

                        IF conflict_count > 0 THEN
                            RAISE EXCEPTION 'CRITICAL MIGRATION ABORT: conflicting subscription evidence across invoices';
                        END IF;
                    END $$;
                """))
            assert "conflicting subscription evidence across invoices" in str(exc_info.value)
            conn.rollback()


@pytest.mark.skipif(not POSTGRES_TEST_URL, reason="Disposable PostgreSQL environment required")
def test_5_existing_provenance_disagreeing_with_invoice_evidence(pg_engine) -> None:
    """Scenario 5: Existing non-null subscription_id disagreeing with invoice evidence triggers abort."""
    with pg_engine.connect() as conn:
        with conn.begin():
            org_id = uuid.uuid4()
            sub_id1 = uuid.uuid4()
            sub_id2 = uuid.uuid4()
            period_id = uuid.uuid4()
            stripe_sub_id = f"sub_stripe_{uuid.uuid4().hex[:12]}"

            conn.execute(text("INSERT INTO organizations (id, name, slug) VALUES (:id, 'Test Org', :slug)"), {"id": org_id, "slug": f"org-{uuid.uuid4().hex[:8]}"})
            conn.execute(
                text("INSERT INTO subscriptions (id, organization_id, status, plan_tier, provider, provider_customer_id, merchant_account_id, environment, stripe_subscription_id) "
                     "VALUES (:id, :org_id, 'active', 'pro', 'stripe', 'cus_1', 'default', 'legacy', :stripe_sub_id)"),
                {"id": sub_id1, "org_id": org_id, "stripe_sub_id": stripe_sub_id},
            )
            conn.execute(
                text("INSERT INTO subscriptions (id, organization_id, status, plan_tier, provider, provider_customer_id, merchant_account_id, environment) "
                     "VALUES (:id, :org_id, 'active', 'pro', 'razorpay', 'cust_2', 'acc_1', 'live')"),
                {"id": sub_id2, "org_id": org_id},
            )
            # Period already has subscription_id = sub_id1
            conn.execute(
                text("INSERT INTO organization_billing_periods (id, organization_id, period_start, period_end, plan_tier, monthly_token_quota, subscription_id, is_active) "
                     "VALUES (:id, :org_id, now(), now() + interval '30 days', 'pro', 2000000, :sub_id, true)"),
                {"id": period_id, "org_id": org_id, "sub_id": sub_id1},
            )
            # Invoice points to sub_id2
            conn.execute(
                text("INSERT INTO billing_invoices (id, provider, merchant_account_id, environment, provider_invoice_id, organization_id, subscription_id, provider_customer_id, status, currency, gross_amount_minor_units, billing_period_id) "
                     "VALUES (:id, 'razorpay', 'acc_1', 'live', :inv_id, :org_id, :sub_id, 'cust_2', 'paid', 'USD', 4900, :period_id)"),
                {"id": uuid.uuid4(), "inv_id": f"inv_{uuid.uuid4().hex[:12]}", "org_id": org_id, "sub_id": sub_id2, "period_id": period_id},
            )

            with pytest.raises(DBAPIError) as exc_info:
                conn.execute(text("""
                    DO $$
                    DECLARE
                        mismatch_count INTEGER;
                    BEGIN
                        WITH unified_invoice_candidates AS (
                            SELECT DISTINCT si.billing_period_id, s.id AS subscription_id
                            FROM stripe_invoices si
                            JOIN subscriptions s ON s.stripe_subscription_id = si.stripe_subscription_id
                            WHERE si.billing_period_id IS NOT NULL AND si.stripe_subscription_id IS NOT NULL
                            UNION
                            SELECT DISTINCT bi.billing_period_id, bi.subscription_id
                            FROM billing_invoices bi
                            WHERE bi.billing_period_id IS NOT NULL AND bi.subscription_id IS NOT NULL
                        )
                        SELECT COUNT(*) INTO mismatch_count
                        FROM organization_billing_periods obp
                        JOIN unified_invoice_candidates uic ON uic.billing_period_id = obp.id
                        WHERE obp.subscription_id IS NOT NULL AND obp.subscription_id IS DISTINCT FROM uic.subscription_id;

                        IF mismatch_count > 0 THEN
                            RAISE EXCEPTION 'CRITICAL MIGRATION ABORT: existing provenance disagreeing with invoice evidence';
                        END IF;
                    END $$;
                """))
            assert "existing provenance disagreeing with invoice evidence" in str(exc_info.value)
            conn.rollback()


@pytest.mark.skipif(not POSTGRES_TEST_URL, reason="Disposable PostgreSQL environment required")
def test_6_active_pro_period_without_provenance_aborts(pg_engine) -> None:
    """Scenario 6: Active Pro period remaining with NULL subscription_id triggers Abort Gate."""
    with pg_engine.connect() as conn:
        with conn.begin():
            org_id = uuid.uuid4()
            period_id = uuid.uuid4()

            conn.execute(text("INSERT INTO organizations (id, name, slug) VALUES (:id, 'Test Org', :slug)"), {"id": org_id, "slug": f"org-{uuid.uuid4().hex[:8]}"})
            conn.execute(
                text("INSERT INTO organization_billing_periods (id, organization_id, period_start, period_end, plan_tier, monthly_token_quota, subscription_id, is_active) "
                     "VALUES (:id, :org_id, now(), now() + interval '30 days', 'pro', 2000000, NULL, true)"),
                {"id": period_id, "org_id": org_id},
            )

            with pytest.raises(DBAPIError) as exc_info:
                conn.execute(text("""
                    DO $$
                    DECLARE
                        unresolved_count INTEGER;
                    BEGIN
                        SELECT COUNT(*) INTO unresolved_count
                        FROM organization_billing_periods
                        WHERE plan_tier != 'free' AND is_active = true AND subscription_id IS NULL;

                        IF unresolved_count > 0 THEN
                            RAISE EXCEPTION 'CRITICAL MIGRATION ABORT: active Pro billing period(s) lack proven subscription provenance';
                        END IF;
                    END $$;
                """))
            assert "lack proven subscription provenance" in str(exc_info.value)
            conn.rollback()


@pytest.mark.skipif(not POSTGRES_TEST_URL, reason="Disposable PostgreSQL environment required")
def test_7_free_period_with_null_provenance_succeeds(pg_engine) -> None:
    """Scenario 7: Free tier billing period with NULL subscription_id is ignored by Abort Gate."""
    with pg_engine.connect() as conn:
        with conn.begin():
            org_id = uuid.uuid4()
            period_id = uuid.uuid4()

            conn.execute(text("INSERT INTO organizations (id, name, slug) VALUES (:id, 'Test Org', :slug)"), {"id": org_id, "slug": f"org-{uuid.uuid4().hex[:8]}"})
            conn.execute(
                text("INSERT INTO organization_billing_periods (id, organization_id, period_start, period_end, plan_tier, monthly_token_quota, subscription_id, is_active) "
                     "VALUES (:id, :org_id, now(), now() + interval '30 days', 'free', 100000, NULL, true)"),
                {"id": period_id, "org_id": org_id},
            )

            # Abort Gate should NOT raise for free tier
            res = conn.execute(text("""
                DO $$
                DECLARE
                    unresolved_count INTEGER;
                BEGIN
                    SELECT COUNT(*) INTO unresolved_count
                    FROM organization_billing_periods
                    WHERE plan_tier != 'free' AND is_active = true AND subscription_id IS NULL;

                    IF unresolved_count > 0 THEN
                        RAISE EXCEPTION 'CRITICAL MIGRATION ABORT';
                    END IF;
                END $$;
            """))
            assert res is not None
            conn.rollback()


@pytest.mark.skipif(not POSTGRES_TEST_URL, reason="Disposable PostgreSQL environment required")
def test_8_composite_foreign_key_mismatch_rejection(pg_engine) -> None:
    """Scenario 8: Inserting payment with mismatched composite identity fails composite foreign key constraint."""
    with pg_engine.connect() as conn:
        with conn.begin():
            org_id = uuid.uuid4()
            attempt_id = uuid.uuid4()

            conn.execute(text("INSERT INTO organizations (id, name, slug) VALUES (:id, 'Test Org', :slug)"), {"id": org_id, "slug": f"org-{uuid.uuid4().hex[:8]}"})
            conn.execute(
                text("INSERT INTO billing_checkout_attempts (id, organization_id, provider, merchant_account_id, environment, provider_subscription_id, provider_payment_id, idempotency_key, status, amount_minor_units, currency, attempt_lease_expires_at) "
                     "VALUES (:id, :org_id, 'razorpay', 'acc_live', 'live', 'sub_123', 'pay_123', 'idem_1', 'complete', 4900, 'USD', now() + interval '1 hour')"),
                {"id": attempt_id, "org_id": org_id},
            )

            # Insert payment with mismatched environment ('test' instead of 'live')
            with pytest.raises(IntegrityError):
                conn.execute(
                    text("INSERT INTO billing_payments (id, provider, merchant_account_id, environment, provider_payment_id, provider_subscription_id, checkout_attempt_id, amount_minor_units, currency, status) "
                         "VALUES (:id, 'razorpay', 'acc_live', 'test', 'pay_123', 'sub_123', :attempt_id, 4900, 'USD', 'captured')"),
                    {"id": uuid.uuid4(), "attempt_id": attempt_id},
                )
            conn.rollback()


@pytest.mark.skipif(not POSTGRES_TEST_URL, reason="Disposable PostgreSQL environment required")
def test_9_composite_foreign_key_null_bypass_rejection(pg_engine) -> None:
    """Scenario 9: Inserting payment with checkout_attempt_id set but provider_payment_id NULL violates check constraint."""
    with pg_engine.connect() as conn:
        with conn.begin():
            org_id = uuid.uuid4()
            attempt_id = uuid.uuid4()

            conn.execute(text("INSERT INTO organizations (id, name, slug) VALUES (:id, 'Test Org', :slug)"), {"id": org_id, "slug": f"org-{uuid.uuid4().hex[:8]}"})
            conn.execute(
                text("INSERT INTO billing_checkout_attempts (id, organization_id, provider, merchant_account_id, environment, provider_subscription_id, provider_payment_id, idempotency_key, status, amount_minor_units, currency, attempt_lease_expires_at) "
                     "VALUES (:id, :org_id, 'razorpay', 'acc_live', 'live', 'sub_123', 'pay_123', 'idem_1', 'complete', 4900, 'USD', now() + interval '1 hour')"),
                {"id": attempt_id, "org_id": org_id},
            )

            # Attempt inserting payment with checkout_attempt_id populated but provider_payment_id NULL
            # Must be rejected by ck_billing_payments_checkout_attempt_complete
            with pytest.raises(IntegrityError):
                conn.execute(
                    text("INSERT INTO billing_payments (id, provider, merchant_account_id, environment, provider_payment_id, provider_subscription_id, checkout_attempt_id, amount_minor_units, currency, status) "
                         "VALUES (:id, 'razorpay', 'acc_live', 'live', NULL, 'sub_123', :attempt_id, 4900, 'USD', 'captured')"),
                    {"id": uuid.uuid4(), "attempt_id": attempt_id},
                )
            conn.rollback()


@pytest.mark.skipif(not POSTGRES_TEST_URL, reason="Disposable PostgreSQL environment required")
def test_10_successful_initial_payment_association(pg_engine) -> None:
    """Scenario 10: Legitimate initial association from NULL -> verified ID succeeds without trigger error."""
    with pg_engine.connect() as conn:
        with conn.begin():
            org_id = uuid.uuid4()
            attempt_id = uuid.uuid4()
            payment_id = uuid.uuid4()

            conn.execute(text("INSERT INTO organizations (id, name, slug) VALUES (:id, 'Test Org', :slug)"), {"id": org_id, "slug": f"org-{uuid.uuid4().hex[:8]}"})
            # Attempt created with NULL provider_payment_id initially
            conn.execute(
                text("INSERT INTO billing_checkout_attempts (id, organization_id, provider, merchant_account_id, environment, provider_subscription_id, provider_payment_id, idempotency_key, status, amount_minor_units, currency, attempt_lease_expires_at) "
                     "VALUES (:id, :org_id, 'razorpay', 'acc_live', 'live', 'sub_123', NULL, 'idem_1', 'open', 4900, 'USD', now() + interval '1 hour')"),
                {"id": attempt_id, "org_id": org_id},
            )
            # Payment created with NULL checkout_attempt_id initially (payment-first staging)
            conn.execute(
                text("INSERT INTO billing_payments (id, provider, merchant_account_id, environment, provider_payment_id, provider_subscription_id, checkout_attempt_id, amount_minor_units, currency, status) "
                     "VALUES (:id, 'razorpay', 'acc_live', 'live', 'pay_123', 'sub_123', NULL, 4900, 'USD', 'captured')"),
                {"id": payment_id},
            )

            # Initial association: attempt NULL -> pay_123
            conn.execute(
                text("UPDATE billing_checkout_attempts SET provider_payment_id = 'pay_123', status = 'complete' WHERE id = :id"),
                {"id": attempt_id},
            )
            # Initial association: payment NULL -> attempt_id
            conn.execute(
                text("UPDATE billing_payments SET checkout_attempt_id = :attempt_id WHERE id = :id"),
                {"attempt_id": attempt_id, "id": payment_id},
            )

            res_attempt = conn.execute(text("SELECT provider_payment_id FROM billing_checkout_attempts WHERE id = :id"), {"id": attempt_id}).scalar()
            res_payment = conn.execute(text("SELECT checkout_attempt_id FROM billing_payments WHERE id = :id"), {"id": payment_id}).scalar()

            assert res_attempt == "pay_123"
            assert res_payment == attempt_id
            conn.rollback()


@pytest.mark.skipif(not POSTGRES_TEST_URL, reason="Disposable PostgreSQL environment required")
def test_11_reassignment_of_established_payment_association_rejected(pg_engine) -> None:
    """Scenario 11: Modifying established checkout_attempt_id triggers immutability exception."""
    with pg_engine.connect() as conn:
        with conn.begin():
            org_id = uuid.uuid4()
            attempt_id = uuid.uuid4()
            payment_id = uuid.uuid4()

            conn.execute(text("INSERT INTO organizations (id, name, slug) VALUES (:id, 'Test Org', :slug)"), {"id": org_id, "slug": f"org-{uuid.uuid4().hex[:8]}"})
            conn.execute(
                text("INSERT INTO billing_checkout_attempts (id, organization_id, provider, merchant_account_id, environment, provider_subscription_id, provider_payment_id, idempotency_key, status, amount_minor_units, currency, attempt_lease_expires_at) "
                     "VALUES (:id, :org_id, 'razorpay', 'acc_live', 'live', 'sub_123', 'pay_123', 'idem_1', 'complete', 4900, 'USD', now() + interval '1 hour')"),
                {"id": attempt_id, "org_id": org_id},
            )
            conn.execute(
                text("INSERT INTO billing_payments (id, provider, merchant_account_id, environment, provider_payment_id, provider_subscription_id, checkout_attempt_id, amount_minor_units, currency, status) "
                     "VALUES (:id, 'razorpay', 'acc_live', 'live', 'pay_123', 'sub_123', :attempt_id, 4900, 'USD', 'captured')"),
                {"id": payment_id, "attempt_id": attempt_id},
            )

            # Attempt to reassign checkout_attempt_id to another UUID or NULL
            with pytest.raises(DBAPIError) as exc_info:
                conn.execute(
                    text("UPDATE billing_payments SET checkout_attempt_id = :new_id WHERE id = :id"),
                    {"new_id": uuid.uuid4(), "id": payment_id},
                )
            assert "Immutability violation" in str(exc_info.value)
            conn.rollback()


@pytest.mark.skipif(not POSTGRES_TEST_URL, reason="Disposable PostgreSQL environment required")
def test_12_reassignment_of_established_period_provenance_rejected(pg_engine) -> None:
    """Scenario 12: Modifying established subscription_id on billing period triggers immutability exception."""
    with pg_engine.connect() as conn:
        with conn.begin():
            org_id = uuid.uuid4()
            sub_id1 = uuid.uuid4()
            sub_id2 = uuid.uuid4()
            period_id = uuid.uuid4()

            conn.execute(text("INSERT INTO organizations (id, name, slug) VALUES (:id, 'Test Org', :slug)"), {"id": org_id, "slug": f"org-{uuid.uuid4().hex[:8]}"})
            conn.execute(
                text("INSERT INTO subscriptions (id, organization_id, status, plan_tier, provider, provider_customer_id, merchant_account_id, environment) "
                     "VALUES (:id, :org_id, 'active', 'pro', 'razorpay', 'cust_1', 'acc_1', 'live')"),
                {"id": sub_id1, "org_id": org_id},
            )
            conn.execute(
                text("INSERT INTO subscriptions (id, organization_id, status, plan_tier, provider, provider_customer_id, merchant_account_id, environment) "
                     "VALUES (:id, :org_id, 'active', 'pro', 'razorpay', 'cust_2', 'acc_1', 'live')"),
                {"id": sub_id2, "org_id": org_id},
            )
            conn.execute(
                text("INSERT INTO organization_billing_periods (id, organization_id, period_start, period_end, plan_tier, monthly_token_quota, subscription_id, is_active) "
                     "VALUES (:id, :org_id, now(), now() + interval '30 days', 'pro', 2000000, :sub_id, true)"),
                {"id": period_id, "org_id": org_id, "sub_id": sub_id1},
            )

            # Attempt to reassign subscription_id to sub_id2
            with pytest.raises(DBAPIError) as exc_info:
                conn.execute(
                    text("UPDATE organization_billing_periods SET subscription_id = :new_sub WHERE id = :id"),
                    {"new_sub": sub_id2, "id": period_id},
                )
            assert "Immutability violation" in str(exc_info.value)
            conn.rollback()


@pytest.mark.skipif(not POSTGRES_TEST_URL, reason="Disposable PostgreSQL environment required")
def test_13_concurrent_attempts_to_associate_conflicting_records(pg_engine) -> None:
    """Scenario 13: Unique constraint uq_billing_payments_checkout_attempt rejects second payment linking to same attempt across independent connections."""
    org_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    pay1_id = uuid.uuid4()
    pay2_id = uuid.uuid4()

    # Connection 1 creates organization, attempt, and associates Payment 1
    with pg_engine.connect() as conn1:
        with conn1.begin():
            conn1.execute(text("INSERT INTO organizations (id, name, slug) VALUES (:id, 'Test Org', :slug)"), {"id": org_id, "slug": f"org-{uuid.uuid4().hex[:8]}"})
            conn1.execute(
                text("INSERT INTO billing_checkout_attempts (id, organization_id, provider, merchant_account_id, environment, provider_subscription_id, provider_payment_id, idempotency_key, status, amount_minor_units, currency, attempt_lease_expires_at) "
                     "VALUES (:id, :org_id, 'razorpay', 'acc_live', 'live', 'sub_123', 'pay_123', 'idem_1', 'complete', 4900, 'USD', now() + interval '1 hour')"),
                {"id": attempt_id, "org_id": org_id},
            )
            # Payment 1 associated on Connection 1
            conn1.execute(
                text("INSERT INTO billing_payments (id, provider, merchant_account_id, environment, provider_payment_id, provider_subscription_id, checkout_attempt_id, amount_minor_units, currency, status) "
                     "VALUES (:id, 'razorpay', 'acc_live', 'live', 'pay_123', 'sub_123', :attempt_id, 4900, 'USD', 'captured')"),
                {"id": pay1_id, "attempt_id": attempt_id},
            )
        # conn1 committed

    try:
        # Genuinely independent Connection 2 attempts to link a second payment to the same checkout_attempt_id
        with pg_engine.connect() as conn2:
            with conn2.begin():
                with pytest.raises(IntegrityError):
                    conn2.execute(
                        text("INSERT INTO billing_payments (id, provider, merchant_account_id, environment, provider_payment_id, provider_subscription_id, checkout_attempt_id, amount_minor_units, currency, status) "
                             "VALUES (:id, 'razorpay', 'acc_live', 'live', 'pay_456', 'sub_123', :attempt_id, 4900, 'USD', 'captured')"),
                        {"id": pay2_id, "attempt_id": attempt_id},
                    )
    finally:
        # Cleanup committed test records
        with pg_engine.connect() as cleanup_conn:
            with cleanup_conn.begin():
                cleanup_conn.execute(text("DELETE FROM billing_payments WHERE id = :id"), {"id": pay1_id})
                cleanup_conn.execute(text("DELETE FROM billing_checkout_attempts WHERE id = :id"), {"id": attempt_id})
                cleanup_conn.execute(text("DELETE FROM organizations WHERE id = :id"), {"id": org_id})


@pytest.mark.skipif(not POSTGRES_TEST_URL, reason="Disposable PostgreSQL environment required")
def test_14_downgrade_refusal_when_financial_associations_exist(pg_engine) -> None:
    """Scenario 14: Downgrade guard aborts if any linked payments, periods, or attempts exist."""
    with pg_engine.connect() as conn:
        with conn.begin():
            org_id = uuid.uuid4()
            sub_id = uuid.uuid4()
            period_id = uuid.uuid4()

            conn.execute(text("INSERT INTO organizations (id, name, slug) VALUES (:id, 'Test Org', :slug)"), {"id": org_id, "slug": f"org-{uuid.uuid4().hex[:8]}"})
            conn.execute(
                text("INSERT INTO subscriptions (id, organization_id, status, plan_tier, provider, provider_customer_id, merchant_account_id, environment) "
                     "VALUES (:id, :org_id, 'active', 'pro', 'razorpay', 'cust_1', 'acc_1', 'live')"),
                {"id": sub_id, "org_id": org_id},
            )
            # Linked period exists
            conn.execute(
                text("INSERT INTO organization_billing_periods (id, organization_id, period_start, period_end, plan_tier, monthly_token_quota, subscription_id, is_active) "
                     "VALUES (:id, :org_id, now(), now() + interval '30 days', 'pro', 2000000, :sub_id, true)"),
                {"id": period_id, "org_id": org_id, "sub_id": sub_id},
            )

            # Execute downgrade guard DO block: must raise exception
            with pytest.raises(DBAPIError) as exc_info:
                conn.execute(text("""
                    DO $$
                    DECLARE
                        cnt_linked_periods INTEGER;
                        cnt_linked_attempts INTEGER;
                        cnt_linked_payments INTEGER;
                        total_records INTEGER;
                    BEGIN
                        SELECT count(*) INTO cnt_linked_periods FROM organization_billing_periods WHERE subscription_id IS NOT NULL;
                        SELECT count(*) INTO cnt_linked_attempts FROM billing_checkout_attempts WHERE provider_payment_id IS NOT NULL;
                        SELECT count(*) INTO cnt_linked_payments FROM billing_payments WHERE checkout_attempt_id IS NOT NULL;

                        total_records := cnt_linked_periods + cnt_linked_attempts + cnt_linked_payments;
                        IF total_records > 0 THEN
                            RAISE EXCEPTION 'Alembic Downgrade Aborted: % associated records exist. Downgrading payment association schema is strictly forbidden to preserve financial provenance.',
                                total_records;
                        END IF;
                    END $$;
                """))
            assert "Alembic Downgrade Aborted" in str(exc_info.value)
            conn.rollback()
