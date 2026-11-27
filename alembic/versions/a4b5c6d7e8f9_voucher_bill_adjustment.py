"""Vouchers: reconciliation date, cost centre / department / employee on a line, and bill-by-bill adjustment of vendor payments.

Revision ID: a4b5c6d7e8f9
Revises: e3f4a5b6c7d8
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a4b5c6d7e8f9"
down_revision = "e3f4a5b6c7d8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("finance_vouchers", sa.Column("reco_date", sa.Date(), nullable=True))
    for name in ("cost_center", "department", "employee"):
        op.add_column("finance_voucher_lines", sa.Column(name, sa.String(), nullable=True))
    op.create_table(
        "finance_voucher_allocations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("line_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("finance_voucher_lines.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("payable_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("payables.id"), nullable=True),
        sa.Column("bill_no", sa.String(), nullable=True),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("deduction", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("remarks", sa.String(), nullable=True),
    )
    op.create_index("ix_finance_voucher_allocations_line", "finance_voucher_allocations", ["line_id"])
    op.create_index("ix_finance_voucher_allocations_payable", "finance_voucher_allocations", ["payable_id"])


def downgrade() -> None:
    op.drop_table("finance_voucher_allocations")
    for name in ("cost_center", "department", "employee"):
        op.drop_column("finance_voucher_lines", name)
    op.drop_column("finance_vouchers", "reco_date")
