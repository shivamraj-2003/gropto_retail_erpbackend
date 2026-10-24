"""Paytm Wireless POS: store terminals and the requests sent to them

Additive only. Razorpay needs no migration: payments rows with method/reference from Razorpay stay as they were
(history is never rewritten), and no Razorpay table or column existed.

Revision ID: f2b3c4d5e6a8
Revises: e9d0e1f2a3b5
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "f2b3c4d5e6a8"
down_revision = "e9d0e1f2a3b5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "paytm_terminals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("store_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("stores.id"), nullable=False),
        sa.Column("mid", sa.String(), nullable=False),
        sa.Column("tid", sa.String(), nullable=False),
        sa.Column("device_serial", sa.String()),
        sa.Column("label", sa.String()),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("mid", "tid", name="uq_paytm_terminal_mid_tid"),
    )
    op.create_index("ix_paytm_terminals_store_id", "paytm_terminals", ["store_id"])
    op.create_table(
        "paytm_pos_payments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("store_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("stores.id"), nullable=False),
        sa.Column("terminal_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("paytm_terminals.id"), nullable=False),
        sa.Column("merchant_transaction_id", sa.String(), nullable=False, unique=True),
        sa.Column("idempotency_key", sa.String(), nullable=False, unique=True),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="created"),
        sa.Column("status_message", sa.String()),
        sa.Column("receipt_label", sa.String()),
        sa.Column("result_code", sa.String()),
        sa.Column("paytm_details", postgresql.JSONB()),
        sa.Column("sale_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sales.id"), unique=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("last_enquiry_at", sa.DateTime(timezone=True)),
        sa.Column("enquiry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_paytm_pos_payments_store_id", "paytm_pos_payments", ["store_id"])
    op.create_index("ix_paytm_pos_payments_terminal_id", "paytm_pos_payments", ["terminal_id"])
    op.create_index("ix_paytm_pos_payments_status", "paytm_pos_payments", ["status"])


def downgrade() -> None:
    op.drop_table("paytm_pos_payments")
    op.drop_table("paytm_terminals")
