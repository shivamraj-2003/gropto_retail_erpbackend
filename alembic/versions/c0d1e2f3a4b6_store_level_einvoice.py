"""store_level_einvoice

Revision ID: c0d1e2f3a4b6
Revises: f1a2b3c4d5e7
Create Date: 2026-10-07 21:00:00.000000

Each Gropto store is its own GST filer (its own GSTIN), so the e-invoice
threshold is judged per store, not per company: stores gain an explicit
"e-invoice applicable" flag and an optional threshold of their own (the
default, ₹5 crore, applies when blank).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c0d1e2f3a4b6"
down_revision: Union[str, None] = "f1a2b3c4d5e7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("stores", sa.Column("einvoice_applicable", sa.Boolean(), server_default=sa.text("false"), nullable=False))
    op.add_column("stores", sa.Column("aato_threshold", sa.Numeric(14, 2), nullable=True))


def downgrade() -> None:
    op.drop_column("stores", "aato_threshold")
    op.drop_column("stores", "einvoice_applicable")
