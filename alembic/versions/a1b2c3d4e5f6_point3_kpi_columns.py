"""point3_kpi_columns

Revision ID: a1b2c3d4e5f6
Revises: 9d4f0c6b1a77
Create Date: 2026-10-01 00:10:00.000000

Point 3 CEO Command Center audit: adds the data-model support that two
blueprint KPIs (Sales/Sq Ft, Target Achievement) and one (Picking/Packing
Time) had zero backing for anywhere in the schema. No defaults/backfill —
NULL until someone sets a real value; dashboard math already null-guards.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = '9d4f0c6b1a77'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("stores", sa.Column("area_sqft", sa.Numeric(10, 2), nullable=True))
    op.add_column("stores", sa.Column("target_revenue_monthly", sa.Numeric(14, 2), nullable=True))
    op.add_column("orders", sa.Column("packed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("orders", "packed_at")
    op.drop_column("stores", "target_revenue_monthly")
    op.drop_column("stores", "area_sqft")
