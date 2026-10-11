"""sale_cash_received

Revision ID: e3f4a5b6c7d9
Revises: e2f3a4b5c6d8
Create Date: 2026-10-11 15:00:00.000000

A cash bill can record the cash the customer handed over and the change given back (both null for other bills).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e3f4a5b6c7d9"
down_revision: Union[str, None] = "e2f3a4b5c6d8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("sales", sa.Column("cash_received", sa.Numeric(12, 2)))
    op.add_column("sales", sa.Column("change_given", sa.Numeric(12, 2)))


def downgrade() -> None:
    op.drop_column("sales", "change_given")
    op.drop_column("sales", "cash_received")
