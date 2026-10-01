"""Point 9 audit fix: pricing/promotion/loyalty gaps — promotion redemption
log, coupons, loyalty point expiry, OMS discount tracking.

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-10-01
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "b8c9d0e1f2a3"
down_revision = "a7b8c9d0e1f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("loyalty_config", sa.Column("points_expiry_days", sa.Integer(), nullable=True))

    op.add_column("orders", sa.Column("discount_total", sa.Numeric(12, 2), nullable=False, server_default="0"))
    op.add_column("orders", sa.Column("coupon_code", sa.String(), nullable=True))

    op.create_table(
        "promotion_redemptions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("promotion_rule_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("promotion_rules.id"), nullable=False),
        sa.Column("source_type", sa.String(), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("store_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("stores.id"), nullable=True),
        sa.Column("discount_amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("details_json", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("source_type", "source_id", "promotion_rule_id", name="uq_promotion_redemption_source"),
    )

    op.create_table(
        "coupons",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("code", sa.String(), nullable=False, unique=True),
        sa.Column("discount_type", sa.String(), nullable=False),
        sa.Column("discount_value", sa.Numeric(12, 2), nullable=False),
        sa.Column("min_cart_value", sa.Numeric(12, 2), nullable=True),
        sa.Column("max_discount_amount", sa.Numeric(12, 2), nullable=True),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("usage_limit_total", sa.Integer(), nullable=True),
        sa.Column("usage_limit_per_customer", sa.Integer(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "coupon_redemptions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("coupon_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("coupons.id"), nullable=False),
        sa.Column("customer_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("customers.id"), nullable=True),
        sa.Column("source_type", sa.String(), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("discount_amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("source_type", "source_id", "coupon_id", name="uq_coupon_redemption_source"),
    )


def downgrade() -> None:
    op.drop_table("coupon_redemptions")
    op.drop_table("coupons")
    op.drop_table("promotion_redemptions")
    op.drop_column("orders", "coupon_code")
    op.drop_column("orders", "discount_total")
    op.drop_column("loyalty_config", "points_expiry_days")
