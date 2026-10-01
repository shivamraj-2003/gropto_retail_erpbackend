"""point8_oms_gaps

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-10-01 06:00:00.000000

Point 8 audit — order idempotency, OTP expiry, distinct pick/pack identities
and timestamps, payment/refund fields on Order, status history, and reverse
logistics (OrderReturn) for online orders.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'a7b8c9d0e1f2'
down_revision: Union[str, None] = 'f6a7b8c9d0e1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("delivery_otp_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("orders", sa.Column("picked_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True))
    op.add_column("orders", sa.Column("picked_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("orders", sa.Column("packed_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True))
    op.add_column("orders", sa.Column("payment_mode", sa.String(), nullable=False, server_default="cod"))
    op.add_column("orders", sa.Column("payment_reference", sa.String(), nullable=True))
    op.add_column("orders", sa.Column("payment_status", sa.String(), nullable=False, server_default="pending"))
    op.add_column("orders", sa.Column("client_idempotency_key", sa.String(), nullable=True))
    op.create_unique_constraint("uq_orders_client_idempotency_key", "orders", ["client_idempotency_key"])

    op.create_table(
        "order_status_history",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("order_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("orders.id", ondelete="CASCADE"), nullable=False),
        sa.Column("from_status", sa.String(), nullable=True),
        sa.Column("to_status", sa.String(), nullable=False),
        sa.Column("changed_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_order_status_history_order_id", "order_status_history", ["order_id"])

    op.create_table(
        "order_refunds",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("order_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("orders.id"), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("method", sa.String(), nullable=False),
        sa.Column("gateway_refund_id", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="initiated"),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_order_refunds_order_id", "order_refunds", ["order_id"])

    op.create_table(
        "order_returns",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("order_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("orders.id"), nullable=False),
        sa.Column("requested_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("refund_total", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_order_returns_order_id", "order_returns", ["order_id"])

    op.create_table(
        "order_return_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("return_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("order_returns.id", ondelete="CASCADE"), nullable=False),
        sa.Column("order_item_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("order_items.id"), nullable=False),
        sa.Column("product_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("quantity", sa.Numeric(12, 3), nullable=False),
        sa.Column("disposition", sa.String(), nullable=False),
        sa.Column("refund_amount", sa.Numeric(12, 2), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("order_return_items")
    op.drop_index("ix_order_returns_order_id", table_name="order_returns")
    op.drop_table("order_returns")
    op.drop_index("ix_order_refunds_order_id", table_name="order_refunds")
    op.drop_table("order_refunds")
    op.drop_index("ix_order_status_history_order_id", table_name="order_status_history")
    op.drop_table("order_status_history")
    op.drop_constraint("uq_orders_client_idempotency_key", "orders", type_="unique")
    op.drop_column("orders", "client_idempotency_key")
    op.drop_column("orders", "payment_status")
    op.drop_column("orders", "payment_reference")
    op.drop_column("orders", "payment_mode")
    op.drop_column("orders", "packed_by")
    op.drop_column("orders", "picked_at")
    op.drop_column("orders", "picked_by")
    op.drop_column("orders", "delivery_otp_expires_at")
