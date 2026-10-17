"""price_variant_stock

Revision ID: e7b8c9d0e1f3
Revises: e6a7b8c9d0e2
Create Date: 2026-10-17 15:00:00.000000

How many units of each extra price a store holds. A share of the store's total stock, not a second stock.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e7b8c9d0e1f3"
down_revision: Union[str, None] = "e6a7b8c9d0e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "price_variant_stock",
        sa.Column("variant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("product_price_variants.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("store_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("stores.id"), primary_key=True),
        sa.Column("quantity", sa.Numeric(14, 3), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("price_variant_stock")
