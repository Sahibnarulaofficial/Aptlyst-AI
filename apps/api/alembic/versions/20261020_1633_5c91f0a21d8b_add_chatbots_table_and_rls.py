"""add_chatbots_table_and_rls

Revision ID: 5c91f0a21d8b
Revises: d0e25ba60860
Create Date: 2026-10-20 16:33:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '5c91f0a21d8b'
down_revision: Union[str, None] = 'd0e25ba60860'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Create table (sa.Enum will automatically create the chatbot_status enum)
    op.create_table(
        'chatbots',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('organization_id', sa.UUID(), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('website_url', sa.String(length=2048), nullable=True),
        sa.Column('welcome_message', sa.Text(), nullable=True),
        sa.Column('instructions', sa.Text(), nullable=True),
        sa.Column('primary_color', sa.String(length=7), nullable=True, server_default='#3b82f6'),
        sa.Column(
            'status',
            sa.Enum('draft', 'active', 'inactive', name='chatbot_status'),
            nullable=False,
            server_default='draft',
        ),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_chatbots_organization_id', 'chatbots', ['organization_id'])
    op.create_index('ix_chatbots_org_created_at', 'chatbots', ['organization_id', 'created_at'])

    # 2. Enable Row-Level Security
    op.execute("ALTER TABLE chatbots ENABLE ROW LEVEL SECURITY")

    # 3. Revoke direct table privileges from untrusted PostgREST client roles
    op.execute("REVOKE ALL ON chatbots FROM anon, authenticated")


def downgrade() -> None:
    # 1. Accurately restore prior default ACL grants
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER "
        "ON chatbots TO anon, authenticated"
    )

    # 2. Disable Row-Level Security
    op.execute("ALTER TABLE chatbots DISABLE ROW LEVEL SECURITY")

    # 3. Drop indexes and table
    op.drop_index('ix_chatbots_org_created_at', table_name='chatbots')
    op.drop_index('ix_chatbots_organization_id', table_name='chatbots')
    op.drop_table('chatbots')

    # 4. Drop enum
    op.execute("DROP TYPE IF EXISTS chatbot_status")
