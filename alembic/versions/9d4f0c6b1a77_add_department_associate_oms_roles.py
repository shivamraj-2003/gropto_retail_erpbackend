"""add_department_associate_oms_roles

Revision ID: 9d4f0c6b1a77
Revises: 7c3e9a1f2b44
Create Date: 2026-10-01 00:05:00.000000

Point 2 audit finding: the blueprint names "Department Heads" (CEO/Head
Office layer), "Associates" (Store layer, distinct from Cashier), and
"Online Operations / Packers / Riders" (Omnichannel OMS layer) as user
types, but none of the five existed as roles — Cashier was the only
floor-staff tier, and any sale.create-holding role could run the entire
OMS pick/pack/dispatch/deliver chain with no OMS-specific role to assign.

Deliberately reuses existing permission codes rather than inventing new
ones (e.g. "oms.operate") — that would mean also changing every OMS
endpoint's require_permission() gate, which is a larger, riskier change
than this audit pass calls for. These roles get real, working access to
the same endpoints the equivalent existing role already uses.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '9d4f0c6b1a77'
down_revision: Union[str, None] = '7c3e9a1f2b44'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


NEW_ROLES = [
    # code, name, max_discount_percent, max_discount_value
    ("department_head", "Department Head", 10, 1000),
    ("associate", "Store Associate", 0, 0),
    ("online_ops", "Online Operations", 0, 0),
    ("packer", "Packer", 0, 0),
    ("rider", "Rider", 0, 0),
]

NEW_ROLE_PERMISSIONS = {
    # Headquarters department lead: visibility + the ability to request
    # changes, not unilateral authority — same tier of trust as Purchase
    # Head/Finance Head, scoped narrower.
    "department_head": ["report.export", "approval.request", "inventory.view"],
    # Below Cashier: can ring a basic sale and look up stock, but no
    # discount or loyalty-redeem authority.
    "associate": ["sale.create", "inventory.view"],
    # Runs the online order queue (create/view/allocate) and can request
    # stock corrections; does not pick/pack/dispatch itself.
    "online_ops": ["sale.create", "inventory.view", "report.export", "approval.request"],
    # Pick/pack: same capability pick_order()/pack equivalents already
    # require (inventory.adjust) for order fulfillment.
    "packer": ["inventory.adjust", "inventory.view"],
    # Dispatch/deliver: same capability dispatch_order()/deliver_order()
    # already require.
    "rider": ["inventory.adjust"],
}


def upgrade() -> None:
    bind = op.get_bind()
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
