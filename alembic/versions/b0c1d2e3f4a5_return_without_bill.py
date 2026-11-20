"""returns without a bill

Revision ID: b0c1d2e3f4a5
Revises: a9b0c1d2e3f4
"""
import sqlalchemy as sa
from alembic import op

revision = "b0c1d2e3f4a5"
down_revision = "a9b0c1d2e3f4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("returns", "sale_id", nullable=True)
    op.alter_column("return_items", "sale_item_id", nullable=True)
    op.add_column("returns", sa.Column("no_bill", sa.Boolean(), server_default="false", nullable=False))


def downgrade() -> None:
    op.drop_column("returns", "no_bill")
