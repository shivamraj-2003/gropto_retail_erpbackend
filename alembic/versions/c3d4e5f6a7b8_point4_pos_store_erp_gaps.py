"""point4_pos_store_erp_gaps

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-10-01 02:00:00.000000

Point 4 audit — closes the structural gaps found in the Store ERP & POS
review: MRP snapshot on sale lines, exchange linkage on returns, denomination
and multi-tender reconciliation on cash shifts/day close, a real stock-count/
cycle-count workflow, gift vouchers, and idempotent wallet-ledger columns.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, None] = 'b2c3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("sale_items", sa.Column("mrp_snapshot", sa.Numeric(12, 2), nullable=True))

    op.add_column("returns", sa.Column("is_exchange", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    op.add_column("returns", sa.Column("exchange_sale_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sales.id"), nullable=True))

    op.add_column("cashier_shifts", sa.Column("denomination_breakdown", postgresql.JSONB(), nullable=True))
    op.add_column("cashier_shifts", sa.Column("tender_breakdown", postgresql.JSONB(), nullable=True))
    op.add_column("day_close", sa.Column("tender_breakdown", postgresql.JSONB(), nullable=True))

    op.add_column("customer_wallet_ledgers", sa.Column("source_type", sa.String(), nullable=False, server_default="manual"))
    op.add_column(
        "customer_wallet_ledgers",
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False, server_default=sa.text("gen_random_uuid()")),
    )
    op.create_unique_constraint(
        "uq_customer_wallet_ledgers_source", "customer_wallet_ledgers", ["source_type", "source_id", "transaction_type"]
    )

    op.create_table(
        "stock_counts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("store_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("stores.id"), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="counting"),
        sa.Column("initiated_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("finalized_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_stock_counts_store_id", "stock_counts", ["store_id"])

    op.create_table(
        "stock_count_lines",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("count_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("stock_counts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("product_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("expected_qty", sa.Numeric(12, 3), nullable=False),
        sa.Column("counted_qty", sa.Numeric(12, 3), nullable=True),
        sa.Column("variance", sa.Numeric(12, 3), nullable=True),
        sa.UniqueConstraint("count_id", "product_id", name="uq_stock_count_lines_count_product"),
    )
    op.create_index("ix_stock_count_lines_count_id", "stock_count_lines", ["count_id"])

    op.create_table(
        "gift_vouchers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("code", sa.String(), nullable=False, unique=True),
        sa.Column("initial_value", sa.Numeric(12, 2), nullable=False),
        sa.Column("balance", sa.Numeric(12, 2), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="active"),
        sa.Column("issued_to_customer_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("customers.id"), nullable=True),
        sa.Column("issued_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("expires_at", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_gift_vouchers_code", "gift_vouchers", ["code"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_gift_vouchers_code", table_name="gift_vouchers")
    op.drop_table("gift_vouchers")
    op.drop_index("ix_stock_count_lines_count_id", table_name="stock_count_lines")
    op.drop_table("stock_count_lines")
    op.drop_index("ix_stock_counts_store_id", table_name="stock_counts")
    op.drop_table("stock_counts")
    op.drop_constraint("uq_customer_wallet_ledgers_source", "customer_wallet_ledgers", type_="unique")
    op.drop_column("customer_wallet_ledgers", "source_id")
    op.drop_column("customer_wallet_ledgers", "source_type")
    op.drop_column("day_close", "tender_breakdown")
    op.drop_column("cashier_shifts", "tender_breakdown")
    op.drop_column("cashier_shifts", "denomination_breakdown")
    op.drop_column("returns", "exchange_sale_id")
    op.drop_column("returns", "is_exchange")
    op.drop_column("sale_items", "mrp_snapshot")
