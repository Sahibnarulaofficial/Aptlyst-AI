"""enable_rls_and_revoke_public_access

Revision ID: d0e25ba60860
Revises: 3ba61d54e4d9
Create Date: 2026-10-19 18:42:55.245203

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd0e25ba60860'
down_revision: Union[str, None] = '3ba61d54e4d9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Enable Row Level Security on Aptlyst tables.
    # Because FastAPI connects as the 'postgres' role (which has rolbypassrls = True),
    # backend access is preserved while direct PostgREST client queries are blocked.
    op.execute("ALTER TABLE organizations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE organization_members ENABLE ROW LEVEL SECURITY")

    # 2. Revoke table privileges from untrusted client roles (anon and authenticated).
    op.execute("REVOKE ALL ON organizations FROM anon, authenticated")
    op.execute("REVOKE ALL ON organization_members FROM anon, authenticated")


def downgrade() -> None:
    # 1. Accurately restore the exact previous table privileges that were assigned
    # by PostgreSQL's default ACL to anon and authenticated roles prior to this migration
    # (SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER),
    # rather than blindly granting ALL (which in PostgreSQL 16 includes MAINTAIN).
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER "
        "ON organizations TO anon, authenticated"
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER "
        "ON organization_members TO anon, authenticated"
    )

    # 2. Disable Row Level Security on Aptlyst tables.
    op.execute("ALTER TABLE organization_members DISABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE organizations DISABLE ROW LEVEL SECURITY")
