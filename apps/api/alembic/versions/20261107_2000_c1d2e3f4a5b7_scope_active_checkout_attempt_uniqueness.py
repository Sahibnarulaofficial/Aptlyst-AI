"""scope_active_checkout_attempt_uniqueness

Revision ID: c1d2e3f4a5b7
Revises: b1c2d3e4f5a6
Create Date: 2026-11-07 20:00:00.000000

Scope active checkout attempt partial uniqueness to:
(organization_id, provider, merchant_account_id, environment)
WHERE status IN ('creating', 'recovery_required', 'open', 'payment_pending')

Guarantees:
1. Replaces global organization_id partial index with provider/merchant/environment scoped index.
2. Performs a pre-creation validation query checking for existing duplicates under the 4-tuple.
3. Preserves all historical Stripe records and completed/failed/expired checkout attempts.
4. Downgrade guard: validates that no multi-scope active attempts exist before attempting to restore
   the global organization_id uniqueness index.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c1d2e3f4a5b7'
down_revision: Union[str, None] = 'b1c2d3e4f5a6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ACTIVE_STATUS_PREDICATE = "status IN ('creating', 'recovery_required', 'open', 'payment_pending')"


def upgrade() -> None:
    # -------------------------------------------------------------------------
    # 1. Pre-validation: Check for duplicate active records under proposed scoped key
    # -------------------------------------------------------------------------
    from alembic import context
    if not context.is_offline_mode():
        conn = op.get_bind()
        if conn is not None:
            duplicate_check_query = sa.text(f"""
                SELECT organization_id, provider, merchant_account_id, environment, count(*) as count
                FROM billing_checkout_attempts
                WHERE {ACTIVE_STATUS_PREDICATE}
                GROUP BY organization_id, provider, merchant_account_id, environment
                HAVING count(*) > 1
            """)
            res = conn.execute(duplicate_check_query)
            duplicates = res.fetchall() if res is not None else []
            if duplicates:
                dup_details = ", ".join(
                    f"(org={row[0]}, provider={row[1]}, merchant={row[2]}, env={row[3]}, count={row[4]})"
                    for row in duplicates
                )
                raise RuntimeError(
                    f"Cannot create scoped unique index uq_org_scoped_active_billing_checkout_attempt. "
                    f"Duplicate active records detected: {dup_details}"
                )

    # -------------------------------------------------------------------------
    # 2. Drop global organization_id index & create scoped index
    # -------------------------------------------------------------------------
    op.drop_index(
        'uq_org_active_billing_checkout_attempt',
        table_name='billing_checkout_attempts',
    )
    op.create_index(
        'uq_org_scoped_active_billing_checkout_attempt',
        'billing_checkout_attempts',
        ['organization_id', 'provider', 'merchant_account_id', 'environment'],
        unique=True,
        postgresql_where=sa.text(ACTIVE_STATUS_PREDICATE),
    )


def downgrade() -> None:
    # -------------------------------------------------------------------------
    # 1. Pre-validation: Check if restoring global index would violate uniqueness
    # -------------------------------------------------------------------------
    from alembic import context
    if not context.is_offline_mode():
        conn = op.get_bind()
        if conn is not None:
            global_duplicate_check_query = sa.text(f"""
                SELECT organization_id, count(*) as count
                FROM billing_checkout_attempts
                WHERE {ACTIVE_STATUS_PREDICATE}
                GROUP BY organization_id
                HAVING count(*) > 1
            """)
            res = conn.execute(global_duplicate_check_query)
            global_duplicates = res.fetchall() if res is not None else []
            if global_duplicates:
                dup_details = ", ".join(
                    f"(org={row[0]}, count={row[1]})"
                    for row in global_duplicates
                )
                raise RuntimeError(
                    f"Cannot downgrade to global index uq_org_active_billing_checkout_attempt. "
                    f"Multiple active checkout attempts exist across scopes for organization(s): {dup_details}. "
                    f"Resolve multi-scope active attempts before downgrading."
                )

    # -------------------------------------------------------------------------
    # 2. Drop scoped index & restore global organization_id index
    # -------------------------------------------------------------------------
    op.drop_index(
        'uq_org_scoped_active_billing_checkout_attempt',
        table_name='billing_checkout_attempts',
    )
    op.create_index(
        'uq_org_active_billing_checkout_attempt',
        'billing_checkout_attempts',
        ['organization_id'],
        unique=True,
        postgresql_where=sa.text(ACTIVE_STATUS_PREDICATE),
    )
