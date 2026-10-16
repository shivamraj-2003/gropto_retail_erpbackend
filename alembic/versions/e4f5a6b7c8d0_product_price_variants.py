"""product_price_variants

Revision ID: e4f5a6b7c8d0
Revises: e3f4a5b6c7d9
Create Date: 2026-10-16 10:00:00.000000

A product can be on sale at more than one price at once (the price was raised and old stock still carries the old
MRP). The product keeps its current selling price and MRP; each extra price is a row here, with a label such as
"Old stock". Billing offers the choice when a product has more than one.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e4f5a6b7c8d0"
down_revision: Union[str, None] = "e3f4a5b6c7d9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "product_price_variants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("product_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("products.id", ondelete="CASCADE"), nullable=False),
        sa.Column("mrp", sa.Numeric(12, 2), nullable=False),
        sa.Column("selling_price", sa.Numeric(12, 2), nullable=False),
        sa.Column("label", sa.String(), nullable=False, server_default="Other price"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_product_price_variants_product", "product_price_variants", ["product_id"])


def downgrade() -> None:
    op.drop_index("ix_product_price_variants_product", table_name="product_price_variants")
    op.drop_table("product_price_variants")
