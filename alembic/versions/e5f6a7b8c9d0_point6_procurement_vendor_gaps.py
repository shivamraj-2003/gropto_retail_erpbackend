"""point6_procurement_vendor_gaps

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-10-01 04:00:00.000000

Point 6 audit — vendor master fields (bank/category/terms/service area),
PO-line discount/tax/cumulative-received tracking, a real VendorInvoice
entity for three-way match + duplicate-invoice prevention, Payable linkage
to invoices with partial-payment support, and vendor performance scoring
fields.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'e5f6a7b8c9d0'
down_revision: Union[str, None] = 'd4e5f6a7b8c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("vendors", sa.Column("category", sa.String(), nullable=True))
    op.add_column("vendors", sa.Column("service_area", sa.String(), nullable=True))
    op.add_column("vendors", sa.Column("credit_days", sa.Integer(), nullable=False, server_default="30"))
    op.add_column("vendors", sa.Column("bank_account_name", sa.String(), nullable=True))
    op.add_column("vendors", sa.Column("bank_account_number", sa.String(), nullable=True))
    op.add_column("vendors", sa.Column("bank_ifsc", sa.String(), nullable=True))
    op.add_column("vendors", sa.Column("bank_name", sa.String(), nullable=True))
    op.create_unique_constraint("uq_vendors_gst_number", "vendors", ["gst_number"])

    op.add_column("purchase_order_items", sa.Column("discount_amount", sa.Numeric(12, 2), nullable=False, server_default="0"))
    op.add_column("purchase_order_items", sa.Column("tax_rate", sa.Numeric(5, 2), nullable=False, server_default="0"))
    op.add_column("purchase_order_items", sa.Column("received_qty", sa.Numeric(12, 3), nullable=False, server_default="0"))

    op.add_column("vendor_performance_snapshots", sa.Column("price_variance_pct", sa.Numeric(6, 2), nullable=False, server_default="0"))
    op.add_column("vendor_performance_snapshots", sa.Column("service_score", sa.Numeric(5, 2), nullable=False, server_default="0"))

    op.create_table(
        "vendor_invoices",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("vendor_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("vendors.id"), nullable=False),
        sa.Column("purchase_order_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("purchase_orders.id"), nullable=True),
        sa.Column("grn_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("grn.id"), nullable=True),
        sa.Column("invoice_number", sa.String(), nullable=False),
        sa.Column("invoice_date", sa.Date(), nullable=False),
        sa.Column("invoice_amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="recorded"),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("vendor_id", "invoice_number", name="uq_vendor_invoice_number"),
    )
    op.create_index("ix_vendor_invoices_vendor_id", "vendor_invoices", ["vendor_id"])

    op.add_column("payables", sa.Column("vendor_invoice_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("vendor_invoices.id"), nullable=True))
    op.add_column("payables", sa.Column("original_amount", sa.Numeric(12, 2), nullable=False, server_default="0"))

    op.create_table(
        "vendor_payments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("payable_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("payables.id"), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("reference", sa.String(), nullable=True),
        sa.Column("requested_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="applied"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_vendor_payments_payable_id", "vendor_payments", ["payable_id"])


def downgrade() -> None:
    op.drop_index("ix_vendor_payments_payable_id", table_name="vendor_payments")
    op.drop_table("vendor_payments")
    op.drop_column("payables", "original_amount")
    op.drop_column("payables", "vendor_invoice_id")
    op.drop_index("ix_vendor_invoices_vendor_id", table_name="vendor_invoices")
    op.drop_table("vendor_invoices")
    op.drop_column("vendor_performance_snapshots", "service_score")
    op.drop_column("vendor_performance_snapshots", "price_variance_pct")
    op.drop_column("purchase_order_items", "received_qty")
    op.drop_column("purchase_order_items", "tax_rate")
    op.drop_column("purchase_order_items", "discount_amount")
    op.drop_constraint("uq_vendors_gst_number", "vendors", type_="unique")
    op.drop_column("vendors", "bank_name")
    op.drop_column("vendors", "bank_ifsc")
    op.drop_column("vendors", "bank_account_number")
    op.drop_column("vendors", "bank_account_name")
    op.drop_column("vendors", "credit_days")
    op.drop_column("vendors", "service_area")
    op.drop_column("vendors", "category")
