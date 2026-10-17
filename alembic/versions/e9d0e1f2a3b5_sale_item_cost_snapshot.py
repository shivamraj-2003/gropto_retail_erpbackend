"""sale_item_cost_snapshot

Revision ID: e9d0e1f2a3b5
Revises: e8c9d0e1f2a4

A sale line sold from an old-price batch that has its own cost remembers that cost, so margin reports use it.
Empty = use the product's cost price, as before.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e9d0e1f2a3b5"
down_revision: Union[str, None] = "e8c9d0e1f2a4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("sale_items", sa.Column("cost_snapshot", sa.Numeric(12, 2), nullable=True))


def downgrade() -> None:
    op.drop_column("sale_items", "cost_snapshot")
