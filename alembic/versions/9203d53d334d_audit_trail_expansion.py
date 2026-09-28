"""audit trail expansion: ip address, reason, per-entity version, audit.view permission

Revision ID: 9203d53d334d
Revises: 2b1a9c3f7e5a
Create Date: 2026-09-28 00:00:00.000000

Extends the existing append-only audit_log (already immutable — update/delete are
revoked at the grant level, see the initial migration) with the fields a real
edit-log needs: the caller's IP address, a human reason where the caller gave one,
and a per-entity version number so a single record's full change history can be
listed in order (1, 2, 3, ...) without re-deriving it from timestamps. Also seeds
audit.view so a dedicated read API can be permission-gated like every other route,
granted to super_admin and admin (the same scope that already reads exports at
GET /reports/approvals-audit).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "9203d53d334d"
down_revision: Union[str, None] = "2b1a9c3f7e5a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("audit_log", sa.Column("ip_address", sa.String(), nullable=True))
    op.add_column("audit_log", sa.Column("reason", sa.String(), nullable=True))
    op.add_column("audit_log", sa.Column("entity_version", sa.Integer(), nullable=True))
    op.create_index("ix_audit_log_entity", "audit_log", ["entity_type", "entity_id"])
    op.create_index("ix_audit_log_created_at", "audit_log", ["created_at"])

    conn = op.get_bind()
    result = conn.execute(sa.text("insert into permissions (code) values ('audit.view') returning id"))
    permission_id = result.scalar_one()
    for role_code in ("super_admin", "admin"):
        role_id = conn.execute(sa.text("select id from roles where code = :c"), {"c": role_code}).scalar_one()
        conn.execute(
            sa.text("insert into role_permissions (role_id, permission_id) values (:r, :p) on conflict do nothing"),
            {"r": role_id, "p": permission_id},
        )


def downgrade() -> None:
    conn = op.get_bind()
    permission_id = conn.execute(sa.text("select id from permissions where code = 'audit.view'")).scalar_one()
    conn.execute(sa.text("delete from role_permissions where permission_id = :p"), {"p": permission_id})
    conn.execute(sa.text("delete from permissions where id = :p"), {"p": permission_id})

    op.drop_index("ix_audit_log_created_at", table_name="audit_log")
    op.drop_index("ix_audit_log_entity", table_name="audit_log")
    op.drop_column("audit_log", "entity_version")
    op.drop_column("audit_log", "reason")
    op.drop_column("audit_log", "ip_address")
