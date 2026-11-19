"""A purchase can be a cash purchase with no tax invoice

Additive: one boolean on purchases, false for every existing row.

Revision ID: f5e6a7b8c9d0
Revises: f4d5e6f7a8ba
"""

import sqlalchemy as sa
from alembic import op

revision = "f5e6a7b8c9d0"
down_revision = "f4d5e6f7a8ba"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("purchases", sa.Column("without_gst", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("purchases", "without_gst")
