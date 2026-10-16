"""transfer_shortage_and_drafts

Revision ID: e5f6a7b8c9d1
Revises: e4f5a6b7c8d0
Create Date: 2026-10-16 18:00:00.000000

A transfer line now keeps what arrived damaged, how much of a shortage or excess was settled and how
(written off, sent back to the sender, ...), so "sent 5, received 4" is accounted for instead of vanishing.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e5f6a7b8c9d1"
down_revision: Union[str, None] = "e4f5a6b7c8d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("transfer_items", sa.Column("damaged_qty", sa.Numeric(12, 3), nullable=False, server_default="0"))
    op.add_column("transfer_items", sa.Column("settled_qty", sa.Numeric(12, 3), nullable=False, server_default="0"))
    op.add_column("transfer_items", sa.Column("resolution", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("transfer_items", "resolution")
    op.drop_column("transfer_items", "settled_qty")
    op.drop_column("transfer_items", "damaged_qty")
