"""manual vouchers, refund mode on returns, a payable for every purchase

Revision ID: d2e3f4a5b6c7
Revises: c1d2e3f4a5b6
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "d2e3f4a5b6c7"
down_revision = "c1d2e3f4a5b6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("returns", sa.Column("refund_mode", sa.String(), server_default="cash", nullable=False))
    op.add_column("returns", sa.Column("device_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("devices.id"), nullable=True))

    op.create_table(
        "finance_vouchers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("store_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("stores.id"), nullable=False, index=True),
        sa.Column("voucher_type", sa.String(), nullable=False),
        sa.Column("voucher_no", sa.String(), nullable=False),
        sa.Column("voucher_date", sa.Date(), nullable=False, index=True),
        sa.Column("main_account", sa.String()),
        sa.Column("main_account_name", sa.String()),
        sa.Column("narration", sa.Text()),
        sa.Column("total_amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("store_id", "voucher_no", name="uq_finance_voucher_no"),
    )
    op.create_table(
        "finance_voucher_lines",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("voucher_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("finance_vouchers.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("dc", sa.String(), nullable=False),
        sa.Column("account_key", sa.String(), nullable=False),
        sa.Column("account_name", sa.String(), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("mode_type", sa.String()),
        sa.Column("mode_no", sa.String()),
        sa.Column("mode_date", sa.Date()),
    )

    # every purchase already recorded gets the payable it should have had (due after the vendor's credit days)
    op.execute(
        """
        insert into payables (id, vendor_id, purchase_id, original_amount, amount_due, due_date, status)
        select gen_random_uuid(), pu.vendor_id, pu.id, pu.total_amount, pu.total_amount,
               coalesce(pu.invoice_date, (pu.created_at at time zone 'Asia/Kolkata')::date) + coalesce(v.credit_days, 0), 'outstanding'
        from purchases pu join vendors v on v.id = pu.vendor_id
        where not exists (select 1 from payables pa where pa.purchase_id = pu.id) and pu.total_amount > 0
        """
    )


def downgrade() -> None:
    op.drop_table("finance_voucher_lines")
    op.drop_table("finance_vouchers")
    op.drop_column("returns", "device_id")
    op.drop_column("returns", "refund_mode")
