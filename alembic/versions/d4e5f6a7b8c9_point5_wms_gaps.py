"""point5_wms_gaps

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-10-01 03:00:00.000000

Point 5 WMS audit — location on batches (zone/rack/bin becomes real), GRN
number, QC reviewer/timestamp, manufacturing date capture, store indent line
items + priority/reason/transfer linkage, vendor debit-note GRN linkage.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'd4e5f6a7b8c9'
down_revision: Union[str, None] = 'c3d4e5f6a7b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("inventory_batches", sa.Column("location_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("warehouse_zone_locations.id"), nullable=True))

    op.add_column("grn", sa.Column("grn_number", sa.String(), nullable=True))
    op.create_unique_constraint("uq_grn_grn_number", "grn", ["grn_number"])

    op.add_column("grn_items", sa.Column("mfg_date", sa.Date(), nullable=True))
    op.add_column("grn_items", sa.Column("qc_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True))
    op.add_column("grn_items", sa.Column("qc_at", sa.DateTime(timezone=True), nullable=True))

    op.add_column("store_indents", sa.Column("requested_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True))
    op.add_column("store_indents", sa.Column("priority", sa.String(), nullable=False, server_default="normal"))
    op.add_column("store_indents", sa.Column("reason", sa.Text(), nullable=True))
    op.add_column("store_indents", sa.Column("transfer_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("transfers.id"), nullable=True))

    op.create_table(
        "store_indent_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("indent_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("store_indents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("product_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("quantity", sa.Numeric(12, 3), nullable=False),
    )
    op.create_index("ix_store_indent_items_indent_id", "store_indent_items", ["indent_id"])

    op.add_column("vendor_debit_credit_notes", sa.Column("grn_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("grn.id"), nullable=True))
    op.add_column("vendor_debit_credit_notes", sa.Column("grn_item_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("grn_items.id"), nullable=True))
    op.add_column("vendor_debit_credit_notes", sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()))


def downgrade() -> None:
    op.drop_column("vendor_debit_credit_notes", "created_at")
    op.drop_column("vendor_debit_credit_notes", "grn_item_id")
    op.drop_column("vendor_debit_credit_notes", "grn_id")
    op.drop_index("ix_store_indent_items_indent_id", table_name="store_indent_items")
    op.drop_table("store_indent_items")
    op.drop_column("store_indents", "transfer_id")
    op.drop_column("store_indents", "reason")
    op.drop_column("store_indents", "priority")
    op.drop_column("store_indents", "requested_by")
    op.drop_column("grn_items", "qc_at")
    op.drop_column("grn_items", "qc_by")
    op.drop_column("grn_items", "mfg_date")
    op.drop_constraint("uq_grn_grn_number", "grn", type_="unique")
    op.drop_column("grn", "grn_number")
    op.drop_column("inventory_batches", "location_id")
