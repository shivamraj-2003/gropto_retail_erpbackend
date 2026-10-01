"""seed_system_admin_role

Revision ID: a4f1c8e0b6d2
Revises: 926cc9af835a
Create Date: 2026-09-30 11:15:00.000000

app/api/deps.py's sees_all_stores() already referenced "system_admin" in its
role-code tuple (blueprint §14: "System Admin — Configuration and user
administration; no silent transaction editing") but, like the other 5 roles
fixed in the previous migration, it was never actually seeded.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a4f1c8e0b6d2'
down_revision: Union[str, None] = '926cc9af835a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PERMISSIONS = ["config.manage", "user.manage", "device.manage"]


def upgrade() -> None:
    bind = op.get_bind()
    role_id = bind.execute(
        sa.text(
            "insert into roles (code, name, max_discount_percent, max_discount_value) "
            "values ('system_admin', 'System Admin', 0, 0) returning id"
        )
    ).scalar_one()
    for perm_code in PERMISSIONS:
        pid = bind.execute(sa.text("select id from permissions where code = :c"), {"c": perm_code}).scalar_one()
        bind.execute(
            sa.text("insert into role_permissions (role_id, permission_id) values (:r, :p)"),
            {"r": role_id, "p": pid},
        )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("delete from role_permissions where role_id in (select id from roles where code = 'system_admin')"))
    bind.execute(sa.text("delete from roles where code = 'system_admin'"))
