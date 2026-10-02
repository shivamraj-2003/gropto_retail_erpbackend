"""Point 17 audit fix: CEO Alerts Engine gaps — ceo_alerts.source_id lets
per-entity conditions (vendor deterioration, transfer discrepancy) dedup
correctly instead of colliding on a global NULL store_id (the vendor
dedup bug found in the audit: only one vendor_deterioration alert could
ever be active system-wide at a time). Composite indexes back the
(alert_type, store_id/source_id, status) filter every alert rule's dedup
guard runs on every 15/30-minute scheduler tick.

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
Create Date: 2026-10-02
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "d5e6f7a8b9c0"
down_revision = "c4d5e6f7a8b9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ceo_alerts", sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_index("ix_ceo_alerts_type_store_status", "ceo_alerts", ["alert_type", "store_id", "status"])
    op.create_index("ix_ceo_alerts_type_source_status", "ceo_alerts", ["alert_type", "source_id", "status"])
    op.create_index("ix_fraud_alerts_rule_store_status", "fraud_alerts", ["rule_code", "store_id", "status"])
    op.create_index(
        "ix_suspicious_billing_logs_store_cashier_status",
        "suspicious_billing_logs",
        ["store_id", "cashier_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_suspicious_billing_logs_store_cashier_status", table_name="suspicious_billing_logs")
    op.drop_index("ix_fraud_alerts_rule_store_status", table_name="fraud_alerts")
    op.drop_index("ix_ceo_alerts_type_source_status", table_name="ceo_alerts")
    op.drop_index("ix_ceo_alerts_type_store_status", table_name="ceo_alerts")
    op.drop_column("ceo_alerts", "source_id")
