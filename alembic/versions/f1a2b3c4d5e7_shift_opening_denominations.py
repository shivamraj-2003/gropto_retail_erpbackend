"""Store the note/coin count a cashier opened the shift with.

Revision ID: f1a2b3c4d5e7
Revises: b9c0d1e2f3a5
Create Date: 2026-10-02
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "f1a2b3c4d5e7"
down_revision = "b9c0d1e2f3a5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("cashier_shifts", sa.Column("opening_denominations", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("cashier_shifts", "opening_denominations")
