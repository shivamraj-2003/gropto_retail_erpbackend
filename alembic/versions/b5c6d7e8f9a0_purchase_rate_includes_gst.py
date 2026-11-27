"""Purchases: whether the rates on the vendor's bill already include GST.

Revision ID: b5c6d7e8f9a0
Revises: a4b5c6d7e8f9
"""

import sqlalchemy as sa
from alembic import op

revision = "b5c6d7e8f9a0"
down_revision = "a4b5c6d7e8f9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("purchases", sa.Column("rate_includes_gst", sa.Boolean(), nullable=False, server_default="false"))


def downgrade() -> None:
    op.drop_column("purchases", "rate_includes_gst")
