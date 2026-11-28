"""Purchases: the bill's tax options (between states, reverse charge, input credit, GST before discount, cash discount after GST).

Revision ID: c6d7e8f9a0b1
Revises: b5c6d7e8f9a0
"""

import sqlalchemy as sa
from alembic import op

revision = "c6d7e8f9a0b1"
down_revision = "b5c6d7e8f9a0"
branch_labels = None
depends_on = None

FLAGS = (("reverse_charge", "false"), ("input_credit", "true"), ("gst_before_discount", "false"), ("cash_discount_after_gst", "false"))


def upgrade() -> None:
    op.add_column("purchases", sa.Column("inter_state", sa.Boolean(), nullable=True))
    for name, default in FLAGS:
        op.add_column("purchases", sa.Column(name, sa.Boolean(), nullable=False, server_default=default))
    op.add_column("purchase_items", sa.Column("taxable_amount", sa.Numeric(12, 2), nullable=True))
    op.add_column("purchase_items", sa.Column("cash_discount", sa.Numeric(12, 2), nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("purchase_items", "cash_discount")
    op.drop_column("purchase_items", "taxable_amount")
    for name, _ in reversed(FLAGS):
        op.drop_column("purchases", name)
    op.drop_column("purchases", "inter_state")
