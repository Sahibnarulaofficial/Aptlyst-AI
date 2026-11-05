"""add_stripe_subscription_id_to_checkout_attempts

Revision ID: e5f6a1b2c3d4
Revises: d4e5f6a1b2c3
Create Date: 2026-11-01 17:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e5f6a1b2c3d4'
down_revision: Union[str, None] = 'd4e5f6a1b2c3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'stripe_checkout_attempts',
        sa.Column('stripe_subscription_id', sa.String(length=255), nullable=True),
    )
    op.create_index(
        'ix_stripe_checkout_attempts_subscription_id',
        'stripe_checkout_attempts',
        ['stripe_subscription_id'],
    )


def downgrade() -> None:
    op.drop_index(
        'ix_stripe_checkout_attempts_subscription_id',
        table_name='stripe_checkout_attempts',
    )
    op.drop_column('stripe_checkout_attempts', 'stripe_subscription_id')
