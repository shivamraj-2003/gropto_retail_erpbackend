"""Point 10 audit fix: finance/GST/e-invoice gaps — IGST/place-of-supply on
sales, Company/Store GST entity linkage, einvoices status table, budget
write path, cash variance escalation, vendor payment/vendor invoice GST and
idempotency fields, finance.gst_configure permission.

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-10-01
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "c9d0e1f2a3b4"
down_revision = "b8c9d0e1f2a3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Companies: PAN + explicit, configurable e-invoice applicability
    op.add_column("companies", sa.Column("pan", sa.String(), nullable=True))
    op.add_column("companies", sa.Column("einvoice_applicable", sa.Boolean(), nullable=False, server_default="false"))
    op.add_column("companies", sa.Column("aato_threshold", sa.Numeric(14, 2), nullable=True))

    # Stores: real GST entity linkage + place-of-supply source
    op.add_column("stores", sa.Column("company_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("companies.id"), nullable=True))
    op.add_column("stores", sa.Column("gstin", sa.String(), nullable=True))
    op.add_column("stores", sa.Column("state", sa.String(), nullable=True))

    # Sales: GSTR-1-ready fields
    op.add_column("sales", sa.Column("place_of_supply", sa.String(), nullable=True))
    op.add_column("sales", sa.Column("document_type", sa.String(), nullable=False, server_default="invoice"))
    op.add_column("sales", sa.Column("customer_gstin", sa.String(), nullable=True))
    op.add_column("sale_items", sa.Column("igst_amount", sa.Numeric(12, 2), nullable=False, server_default="0"))

    # Vendor invoices: purchase-side GST breakdown (informational)
    op.add_column("vendor_invoices", sa.Column("taxable_value", sa.Numeric(12, 2), nullable=True))
    op.add_column("vendor_invoices", sa.Column("cgst_amount", sa.Numeric(12, 2), nullable=True))
    op.add_column("vendor_invoices", sa.Column("sgst_amount", sa.Numeric(12, 2), nullable=True))
    op.add_column("vendor_invoices", sa.Column("igst_amount", sa.Numeric(12, 2), nullable=True))
    op.add_column("vendor_invoices", sa.Column("place_of_supply", sa.String(), nullable=True))

    # Vendor payments: idempotency
    op.add_column("vendor_payments", sa.Column("idempotency_key", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_unique_constraint("uq_vendor_payments_idempotency_key", "vendor_payments", ["idempotency_key"])

    # Cashier shifts: variance classification/escalation
    op.add_column(
        "cashier_shifts", sa.Column("variance_status", sa.String(), nullable=False, server_default="within_tolerance")
    )
    op.add_column("cashier_shifts", sa.Column("variance_reason", sa.Text(), nullable=True))

    # Store budgets: real write path support
    op.add_column("store_budgets", sa.Column("actual_capex", sa.Numeric(12, 2), nullable=False, server_default="0"))
    op.add_column("store_budgets", sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True))
    op.add_column("store_budgets", sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()))
    op.add_column("store_budgets", sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()))
    # Dedupe any pre-existing rows before the unique constraint (only ever
    # possible via direct DB insert — no endpoint has ever created one).
    op.execute(
        """
        delete from store_budgets a using store_budgets b
        where a.id < b.id
          and a.store_id = b.store_id
          and a.financial_year = b.financial_year
          and a.month = b.month
        """
    )
    op.create_unique_constraint("uq_store_budget_period", "store_budgets", ["store_id", "financial_year", "month"])

    # E-invoices
    op.create_table(
        "einvoices",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("sale_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sales.id"), nullable=False, unique=True),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("companies.id"), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="not_applicable"),
        sa.Column("payload_json", postgresql.JSONB(), nullable=True),
        sa.Column("irn", sa.String(), nullable=True, unique=True),
        sa.Column("ack_no", sa.String(), nullable=True),
        sa.Column("ack_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("signed_qr_code", sa.Text(), nullable=True),
        sa.Column("error_response", sa.Text(), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("idempotency_key", postgresql.UUID(as_uuid=True), nullable=False, unique=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("cancelled_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_einvoices_status", "einvoices", ["status"])

    # Finance RBAC: a dedicated GST-config permission (previously none existed)
    conn = op.get_bind()
    permission_id = conn.execute(
        sa.text("insert into permissions (id, code) values (gen_random_uuid(), 'finance.gst_configure') returning id")
    ).scalar_one()
    for role_code in ("super_admin", "admin", "finance_head"):
        role_id = conn.execute(sa.text("select id from roles where code = :c"), {"c": role_code}).scalar_one_or_none()
        if role_id is not None:
            conn.execute(
                sa.text("insert into role_permissions (role_id, permission_id) values (:r, :p) on conflict do nothing"),
                {"r": role_id, "p": permission_id},
            )


def downgrade() -> None:
    conn = op.get_bind()
    permission_id = conn.execute(sa.text("select id from permissions where code = 'finance.gst_configure'")).scalar_one_or_none()
    if permission_id is not None:
        conn.execute(sa.text("delete from role_permissions where permission_id = :p"), {"p": permission_id})
        conn.execute(sa.text("delete from permissions where id = :p"), {"p": permission_id})

    op.drop_index("ix_einvoices_status", table_name="einvoices")
    op.drop_table("einvoices")

    op.drop_constraint("uq_store_budget_period", "store_budgets", type_="unique")
    op.drop_column("store_budgets", "updated_at")
    op.drop_column("store_budgets", "created_at")
    op.drop_column("store_budgets", "created_by")
    op.drop_column("store_budgets", "actual_capex")

    op.drop_column("cashier_shifts", "variance_reason")
    op.drop_column("cashier_shifts", "variance_status")

    op.drop_constraint("uq_vendor_payments_idempotency_key", "vendor_payments", type_="unique")
    op.drop_column("vendor_payments", "idempotency_key")

    op.drop_column("vendor_invoices", "place_of_supply")
    op.drop_column("vendor_invoices", "igst_amount")
    op.drop_column("vendor_invoices", "sgst_amount")
    op.drop_column("vendor_invoices", "cgst_amount")
    op.drop_column("vendor_invoices", "taxable_value")

    op.drop_column("sale_items", "igst_amount")
    op.drop_column("sales", "customer_gstin")
    op.drop_column("sales", "document_type")
    op.drop_column("sales", "place_of_supply")

    op.drop_column("stores", "state")
    op.drop_column("stores", "gstin")
    op.drop_column("stores", "company_id")

    op.drop_column("companies", "aato_threshold")
    op.drop_column("companies", "einvoice_applicable")
    op.drop_column("companies", "pan")
