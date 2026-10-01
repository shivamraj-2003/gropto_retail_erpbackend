"""point3_remaining_kpis

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-10-01 01:00:00.000000

Point 3 audit — closes the remaining KPI gaps: separate Picking/Packing/
Dispatch stage timestamps on orders, and a manual daily footfall log (no
visitor-counting hardware exists, so this is the honest real data source
for Conversion %).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("picking_started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("orders", sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True))

    op.create_table(
        "store_footfall",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("store_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("stores.id"), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("footfall_count", sa.Integer(), nullable=False),
        sa.Column("recorded_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("store_id", "business_date", name="uq_store_footfall_store_date"),
    )
    op.create_index("ix_store_footfall_store_id", "store_footfall", ["store_id"])


def downgrade() -> None:
    op.drop_index("ix_store_footfall_store_id", table_name="store_footfall")
    op.drop_table("store_footfall")
    op.drop_column("orders", "dispatched_at")
    op.drop_column("orders", "picking_started_at")
