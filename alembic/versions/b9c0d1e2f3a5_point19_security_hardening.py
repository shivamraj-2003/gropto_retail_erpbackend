"""point19_security_hardening

Revision ID: b9c0d1e2f3a5
Revises: a8b9c0d1e2f4
Create Date: 2026-10-02 14:00:00.000000

* roles.mfa_required — seeded true for every privileged role; holders must
  enrol TOTP before the API serves them (Super Admins always must).
* users.must_change_password — set for any account still on a known seed
  password (the defaults published in tests/conftest.py and scripts/seed*.py).
* audit_log made append-only at the database level with triggers. Unlike
  the earlier REVOKE, a trigger also binds the table owner — the role the app
  itself connects as — so UPDATE/DELETE/TRUNCATE fail for everyone short of
  someone deliberately disabling the trigger.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.core.security import verify_password

revision: str = "b9c0d1e2f3a5"
down_revision: Union[str, None] = "a8b9c0d1e2f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

MFA_REQUIRED_ROLES = ["super_admin", "admin", "system_admin", "ceo", "coo", "finance_head", "purchase_head"]
KNOWN_DEFAULT_PASSWORDS = ["ChangeMe!123", "Test@123"]


def upgrade() -> None:
    bind = op.get_bind()
    op.add_column("roles", sa.Column("mfa_required", sa.Boolean(), server_default=sa.text("false"), nullable=False))
    op.add_column("users", sa.Column("must_change_password", sa.Boolean(), server_default=sa.text("false"), nullable=False))
    bind.execute(sa.text("update roles set mfa_required = true where code = any(:codes)"), {"codes": MFA_REQUIRED_ROLES})

    for user_id, password_hash in bind.execute(sa.text("select id, password_hash from users")).all():
        if any(verify_password(p, password_hash) for p in KNOWN_DEFAULT_PASSWORDS):
            bind.execute(sa.text("update users set must_change_password = true where id = :id"), {"id": user_id})

    bind.execute(
        sa.text(
            """
            create or replace function audit_log_append_only() returns trigger
            language plpgsql as $$
            begin
                raise exception 'audit_log is append-only: % is not allowed', tg_op;
            end;
            $$
            """
        )
    )
    bind.execute(sa.text("drop trigger if exists trg_audit_log_append_only on audit_log"))
    bind.execute(sa.text("drop trigger if exists trg_audit_log_no_truncate on audit_log"))
    bind.execute(
        sa.text(
            "create trigger trg_audit_log_append_only before update or delete on audit_log "
            "for each row execute function audit_log_append_only()"
        )
    )
    bind.execute(
        sa.text(
            "create trigger trg_audit_log_no_truncate before truncate on audit_log "
            "for each statement execute function audit_log_append_only()"
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("drop trigger if exists trg_audit_log_no_truncate on audit_log"))
    bind.execute(sa.text("drop trigger if exists trg_audit_log_append_only on audit_log"))
    bind.execute(sa.text("drop function if exists audit_log_append_only()"))
    op.drop_column("users", "must_change_password")
    op.drop_column("roles", "mfa_required")
