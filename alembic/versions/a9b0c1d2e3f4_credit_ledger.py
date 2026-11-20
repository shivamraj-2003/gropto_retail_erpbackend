"""Credit (udhaar): a customer's credit account per store, and an optional credit limit

Additive: one new table and one nullable column.

Revision ID: a9b0c1d2e3f4
Revises: f5e6a7b8c9d0
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a9b0c1d2e3f4"
down_revision = "f5e6a7b8c9d0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("customers", sa.Column("credit_limit", sa.Numeric(12, 2)))
    op.create_table(
        "credit_entries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("customer_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("customers.id"), nullable=False),
        sa.Column("store_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("stores.id"), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("mode", sa.String()),
        sa.Column("sale_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sales.id")),
        sa.Column("note", sa.Text()),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("sale_id", "kind", name="uq_credit_entry_sale_kind"),
    )
    op.create_index("ix_credit_entries_customer_id", "credit_entries", ["customer_id"])
    op.create_index("ix_credit_entries_store_id", "credit_entries", ["store_id"])


def downgrade() -> None:
    op.drop_table("credit_entries")
    op.drop_column("customers", "credit_limit")
