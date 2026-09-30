"""enterprise_roles_and_regions

Revision ID: 926cc9af835a
Revises: d0f72fd50650
Create Date: 2026-09-30 11:00:00.000000

Fixes two confirmed compliance gaps from the blueprint audit:
1. Only 5 of the blueprint's 9 roles were ever seeded (ceo/coo/finance_head/
   purchase_head/regional_manager were referenced in app/api/deps.py's
   sees_all_stores() but never inserted as actual rows) — this seeds them.
2. `audit.view` permission was required by 4 audit endpoints but never
   inserted into the permissions table, so Audit Trail 403'd for every role
   except super_admin (which bypasses the permission check entirely).

Also adds a real `regions` master table + `stores.region_id` FK, replacing
the free-text-only `stores.cluster` column with an actual queryable
hierarchy for the CEO dashboard's City/Cluster rollup.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = '926cc9af835a'
down_revision: Union[str, None] = 'd0f72fd50650'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


NEW_ROLES = [
    # code, name, max_discount_percent, max_discount_value
    ("ceo", "CEO", 50, 10000),
    ("coo", "COO / Operations Head", 30, 8000),
    ("finance_head", "Finance Head", 20, 5000),
    ("purchase_head", "Purchase Head", 20, 5000),
    ("regional_manager", "Regional / Cluster Manager", 15, 2000),
]

NEW_PERMISSION = "audit.view"

# Existing permission codes each new role is granted, per the blueprint's
# section-14 "typical access" column.
NEW_ROLE_PERMISSIONS = {
    "ceo": [
        "report.export", "approval.decide", "audit.view", "config.manage",
        "inventory.view", "user.manage",
    ],
    "coo": [
        "inventory.adjust", "inventory.view", "purchase.manage", "vendor.manage",
        "approval.decide", "report.export", "sale.void", "audit.view",
    ],
    "finance_head": [
        "report.export", "approval.decide", "audit.view", "config.manage",
    ],
    "purchase_head": [
        "vendor.manage", "purchase.manage", "report.export", "approval.decide",
    ],
    "regional_manager": [
        "inventory.view", "inventory.adjust", "report.export", "approval.request",
    ],
}

# audit.view also needs granting to the two roles that already exist and
# plausibly need it (admin was already missing it too — same bug).
EXISTING_ROLE_NEW_PERMISSIONS = {
    "admin": ["audit.view"],
}


def upgrade() -> None:
    bind = op.get_bind()

    op.create_table(
        "regions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("code", sa.String(), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.add_column("stores", sa.Column("region_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("regions.id"), nullable=True))

    # audit.view: insert if missing (it was simply never seeded).
    existing = bind.execute(sa.text("select id from permissions where code = :c"), {"c": NEW_PERMISSION}).scalar_one_or_none()
    if existing is None:
        perm_id = bind.execute(
            sa.text("insert into permissions (code) values (:c) returning id"), {"c": NEW_PERMISSION}
        ).scalar_one()
    else:
        perm_id = existing

    for role_code, perms in EXISTING_ROLE_NEW_PERMISSIONS.items():
        role_id = bind.execute(sa.text("select id from roles where code = :c"), {"c": role_code}).scalar_one()
        for perm_code in perms:
            pid = bind.execute(sa.text("select id from permissions where code = :c"), {"c": perm_code}).scalar_one()
            already = bind.execute(
                sa.text("select 1 from role_permissions where role_id = :r and permission_id = :p"),
                {"r": role_id, "p": pid},
            ).scalar_one_or_none()
            if already is None:
                bind.execute(
                    sa.text("insert into role_permissions (role_id, permission_id) values (:r, :p)"),
                    {"r": role_id, "p": pid},
                )

    role_ids: dict[str, str] = {}
    for code, name, pct, val in NEW_ROLES:
        result = bind.execute(
            sa.text(
                "insert into roles (code, name, max_discount_percent, max_discount_value) "
                "values (:code, :name, :pct, :val) returning id"
            ),
            {"code": code, "name": name, "pct": pct, "val": val},
        )
        role_ids[code] = result.scalar_one()

    for role_code, perms in NEW_ROLE_PERMISSIONS.items():
        for perm_code in perms:
            pid = bind.execute(sa.text("select id from permissions where code = :c"), {"c": perm_code}).scalar_one()
            bind.execute(
                sa.text("insert into role_permissions (role_id, permission_id) values (:r, :p)"),
                {"r": role_ids[role_code], "p": pid},
            )


def downgrade() -> None:
    bind = op.get_bind()
    for code, *_ in NEW_ROLES:
        bind.execute(sa.text("delete from role_permissions where role_id in (select id from roles where code = :c)"), {"c": code})
        bind.execute(sa.text("delete from roles where code = :c"), {"c": code})
    op.drop_column("stores", "region_id")
    op.drop_table("regions")
