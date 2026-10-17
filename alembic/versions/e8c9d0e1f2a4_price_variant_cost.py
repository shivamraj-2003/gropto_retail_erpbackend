"""price_variant_cost

Revision ID: e8c9d0e1f2a4
Revises: e7b8c9d0e1f3

Each extra price (old stock) can carry its own cost price. Empty = same cost as the product.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e8c9d0e1f2a4"
down_revision: Union[str, None] = "e7b8c9d0e1f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("product_price_variants", sa.Column("purchase_price", sa.Numeric(12, 2), nullable=True))


def downgrade() -> None:
    op.drop_column("product_price_variants", "purchase_price")
