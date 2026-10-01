"""point7_inventory_intelligence_gaps

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-10-01 05:00:00.000000

Point 7 audit — an index on inventory_movements for the store/product-scoped
queries (reconciliation, sell-through, shrinkage) that previously had to scan
the full table.
"""
from typing import Sequence, Union

from alembic import op


revision: str = 'f6a7b8c9d0e1'
down_revision: Union[str, None] = 'e5f6a7b8c9d0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_inventory_movements_store_product_created",
        "inventory_movements",
        ["store_id", "product_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_inventory_movements_store_product_created", table_name="inventory_movements")
