"""add_conversations_and_messages

Revision ID: a1b2c3d4e5f6
Revises: 5c91f0a21d8b
Create Date: 2026-10-26 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = '5c91f0a21d8b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Add unique constraint to chatbots so (chatbot_id, organization_id) can be targeted by composite FK
    op.create_unique_constraint('uq_chatbots_id_org_id', 'chatbots', ['id', 'organization_id'])

    # 2. Create conversations table
    op.create_table(
        'conversations',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('organization_id', sa.UUID(), nullable=False),
        sa.Column('chatbot_id', sa.UUID(), nullable=False),
        sa.Column('visitor_id', sa.String(length=64), nullable=False),
        sa.Column(
            'status',
            sa.Enum('active', 'closed', 'archived', name='conversation_status'),
            nullable=False,
            server_default='active',
        ),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(
            ['chatbot_id', 'organization_id'],
            ['chatbots.id', 'chatbots.organization_id'],
            ondelete='CASCADE',
            name='fk_conversations_chatbot_org',
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_conversations_visitor_id', 'conversations', ['visitor_id'])
    op.create_index('ix_conversations_org_bot_created', 'conversations', ['organization_id', 'chatbot_id', 'created_at'])
    op.create_index('ix_conversations_chatbot_visitor', 'conversations', ['chatbot_id', 'visitor_id'])

    # 3. Create messages table
    op.create_table(
        'messages',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('conversation_id', sa.UUID(), nullable=False),
        sa.Column(
            'sender_type',
            sa.Enum('visitor', 'assistant', 'system', name='message_sender_type'),
            nullable=False,
        ),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('client_message_id', sa.String(length=64), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('conversation_id', 'client_message_id', name='uq_messages_conversation_client_id'),
    )
    op.create_index('ix_messages_conversation_id', 'messages', ['conversation_id'])
    op.create_index('ix_messages_conv_created', 'messages', ['conversation_id', 'created_at'])

    # 4. Enable Row-Level Security
    op.execute("ALTER TABLE conversations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE messages ENABLE ROW LEVEL SECURITY")

    # 5. Revoke direct table privileges from untrusted PostgREST client roles
    op.execute("REVOKE ALL ON conversations FROM anon, authenticated")
    op.execute("REVOKE ALL ON messages FROM anon, authenticated")


def downgrade() -> None:
    # 1. Restore prior default ACL grants
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER "
        "ON messages TO anon, authenticated"
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER "
        "ON conversations TO anon, authenticated"
    )

    # 2. Disable Row-Level Security
    op.execute("ALTER TABLE messages DISABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE conversations DISABLE ROW LEVEL SECURITY")

    # 3. Drop tables and indexes
    op.drop_index('ix_messages_conv_created', table_name='messages')
    op.drop_index('ix_messages_conversation_id', table_name='messages')
    op.drop_table('messages')

    op.drop_index('ix_conversations_chatbot_visitor', table_name='conversations')
    op.drop_index('ix_conversations_org_bot_created', table_name='conversations')
    op.drop_index('ix_conversations_visitor_id', table_name='conversations')
    op.drop_table('conversations')

    # 4. Drop enums
    op.execute("DROP TYPE IF EXISTS message_sender_type")
    op.execute("DROP TYPE IF EXISTS conversation_status")

    # 5. Drop unique constraint on chatbots
    op.drop_constraint('uq_chatbots_id_org_id', 'chatbots', type_='unique')
