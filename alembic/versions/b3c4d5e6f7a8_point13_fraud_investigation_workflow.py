"""Point 13 audit fix: fraud/CEO-alert/suspicious-billing investigation
workflow. Previously FraudAlert.status, CeoAlert.status and
SuspiciousBillingLog.reviewed were write-once-at-creation columns with no
endpoint anywhere that ever flipped them — an alert could never be
assigned, investigated, or resolved. Adds assigned_to/resolved_by/
resolved_at/resolution_note to all three tables (and a proper status column
to suspicious_billing_logs, replacing the binary `reviewed` flag with the
same open/reviewed/dismissed vocabulary the other two tables use), plus
dedicated fraud.view/fraud.manage permissions so these endpoints stop being
gated by the unrelated generic approval.decide permission.

Revision ID: b3c4d5e6f7a8
Revises: f7a8b9c0d1e2
Create Date: 2026-10-02
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "b3c4d5e6f7a8"
down_revision = "f7a8b9c0d1e2"
branch_labels = None
depends_on = None


def _add_workflow_columns(table: str) -> None:
    op.add_column(table, sa.Column("assigned_to", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True))
    op.add_column(table, sa.Column("resolved_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True))
    op.add_column(table, sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(table, sa.Column("resolution_note", sa.Text(), nullable=True))


def upgrade() -> None:
    _add_workflow_columns("fraud_alerts")
    op.create_index("ix_fraud_alerts_status", "fraud_alerts", ["status"])

    _add_workflow_columns("ceo_alerts")
    op.create_index("ix_ceo_alerts_status", "ceo_alerts", ["status"])

    op.add_column("suspicious_billing_logs", sa.Column("status", sa.String(), nullable=False, server_default="open"))
    _add_workflow_columns("suspicious_billing_logs")
    op.create_index("ix_suspicious_billing_logs_status", "suspicious_billing_logs", ["status"])
    # Backfill from the old binary flag so existing rows aren't silently reset.
    op.execute("update suspicious_billing_logs set status = 'reviewed' where reviewed is true")

    conn = op.get_bind()

    fraud_view_id = conn.execute(
        sa.text("insert into permissions (id, code) values (gen_random_uuid(), 'fraud.view') returning id")
    ).scalar_one()
    fraud_manage_id = conn.execute(
        sa.text("insert into permissions (id, code) values (gen_random_uuid(), 'fraud.manage') returning id")
    ).scalar_one()

    audit_view_id = conn.execute(sa.text("select id from permissions where code = 'audit.view'")).scalar_one_or_none()
    if audit_view_id is not None:
        conn.execute(
            sa.text(
                "insert into role_permissions (role_id, permission_id) "
                "select role_id, :new_perm from role_permissions where permission_id = :old_perm "
                "on conflict do nothing"
            ),
            {"new_perm": fraud_view_id, "old_perm": audit_view_id},
        )

    approval_decide_id = conn.execute(sa.text("select id from permissions where code = 'approval.decide'")).scalar_one_or_none()
    if approval_decide_id is not None:
        conn.execute(
            sa.text(
                "insert into role_permissions (role_id, permission_id) "
                "select role_id, :new_perm from role_permissions where permission_id = :old_perm "
                "on conflict do nothing"
            ),
            {"new_perm": fraud_manage_id, "old_perm": approval_decide_id},
        )
        # fraud.manage holders can obviously also view what they manage.
        conn.execute(
            sa.text(
                "insert into role_permissions (role_id, permission_id) "
                "select role_id, :new_perm from role_permissions where permission_id = :old_perm "
                "on conflict do nothing"
            ),
            {"new_perm": fraud_view_id, "old_perm": approval_decide_id},
        )


def downgrade() -> None:
    conn = op.get_bind()
    for code in ("fraud.view", "fraud.manage"):
        permission_id = conn.execute(sa.text("select id from permissions where code = :c"), {"c": code}).scalar_one_or_none()
        if permission_id is not None:
            conn.execute(sa.text("delete from role_permissions where permission_id = :p"), {"p": permission_id})
            conn.execute(sa.text("delete from permissions where id = :p"), {"p": permission_id})

    op.drop_index("ix_suspicious_billing_logs_status", table_name="suspicious_billing_logs")
    op.drop_column("suspicious_billing_logs", "resolution_note")
    op.drop_column("suspicious_billing_logs", "resolved_at")
    op.drop_column("suspicious_billing_logs", "resolved_by")
    op.drop_column("suspicious_billing_logs", "assigned_to")
    op.drop_column("suspicious_billing_logs", "status")

    op.drop_index("ix_ceo_alerts_status", table_name="ceo_alerts")
    op.drop_column("ceo_alerts", "resolution_note")
    op.drop_column("ceo_alerts", "resolved_at")
    op.drop_column("ceo_alerts", "resolved_by")
    op.drop_column("ceo_alerts", "assigned_to")

    op.drop_index("ix_fraud_alerts_status", table_name="fraud_alerts")
    op.drop_column("fraud_alerts", "resolution_note")
    op.drop_column("fraud_alerts", "resolved_at")
    op.drop_column("fraud_alerts", "resolved_by")
    op.drop_column("fraud_alerts", "assigned_to")
