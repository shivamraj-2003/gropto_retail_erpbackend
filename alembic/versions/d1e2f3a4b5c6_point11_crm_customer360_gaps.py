"""Point 11 audit fix: CRM/Customer 360 gaps — missing ClvSnapshot,
SavedAudience, ConsentHistory tables (referenced by live code but never
defined, causing every CLV/audience/consent-history endpoint to fail),
customer email/push_token fields for the email/push campaign channels,
ticket assignment fields, campaign audience linkage, and idempotent
campaign-recipient delivery.

Revision ID: d1e2f3a4b5c6
Revises: c9d0e1f2a3b4
Create Date: 2026-10-01
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "d1e2f3a4b5c6"
down_revision = "c9d0e1f2a3b4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Customers: email/push device token (campaign channels had nowhere to send to)
    op.add_column("customers", sa.Column("email", sa.String(), nullable=True))
    op.add_column("customers", sa.Column("push_token", sa.String(), nullable=True))

    # Customer service tickets: assignment/resolution fields the (already
    # frontend-called) update endpoint needed
    op.add_column("customer_service_tickets", sa.Column("assigned_to", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True))
    op.add_column("customer_service_tickets", sa.Column("resolution_notes", sa.Text(), nullable=True))
    op.add_column("customer_service_tickets", sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True))

    # Saved audiences (must exist before campaigns.saved_audience_id FK)
    op.create_table(
        "saved_audiences",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("criteria", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("estimated_size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # Campaigns: link to a saved audience, and record who created it
    op.add_column("campaigns", sa.Column("saved_audience_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("saved_audiences.id"), nullable=True))
    op.add_column("campaigns", sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True))

    # Campaign recipients: idempotent delivery — dedupe any pre-existing
    # rows (only ever possible via the old, duplicate-prone send path)
    # before adding the constraint.
    op.execute(
        """
        delete from campaign_recipients a using campaign_recipients b
        where a.id < b.id and a.campaign_id = b.campaign_id and a.customer_id = b.customer_id
        """
    )
    op.create_unique_constraint("uq_campaign_recipient", "campaign_recipients", ["campaign_id", "customer_id"])

    # CLV snapshots
    op.create_table(
        "clv_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("customer_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("customers.id"), nullable=False, unique=True),
        sa.Column("historic_clv", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("predicted_clv", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("avg_order_value", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("purchase_frequency", sa.Numeric(10, 4), nullable=False, server_default="0"),
        sa.Column("customer_lifespan_months", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("segment", sa.String(), nullable=False, server_default="new"),
        sa.Column("calculated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # Consent change history
    op.create_table(
        "consent_history",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("customer_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("customers.id"), nullable=False),
        sa.Column("channel", sa.String(), nullable=False),
        sa.Column("old_value", sa.Boolean(), nullable=True),
        sa.Column("new_value", sa.Boolean(), nullable=False),
        sa.Column("changed_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("source", sa.String(), nullable=False, server_default="admin"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_consent_history_customer_id", "consent_history", ["customer_id"])


def downgrade() -> None:
    op.drop_index("ix_consent_history_customer_id", table_name="consent_history")
    op.drop_table("consent_history")
    op.drop_table("clv_snapshots")
    op.drop_constraint("uq_campaign_recipient", "campaign_recipients", type_="unique")
    op.drop_column("campaigns", "created_by")
    op.drop_column("campaigns", "saved_audience_id")
    op.drop_table("saved_audiences")
    op.drop_column("customer_service_tickets", "resolved_at")
    op.drop_column("customer_service_tickets", "resolution_notes")
    op.drop_column("customer_service_tickets", "assigned_to")
    op.drop_column("customers", "push_token")
    op.drop_column("customers", "email")
