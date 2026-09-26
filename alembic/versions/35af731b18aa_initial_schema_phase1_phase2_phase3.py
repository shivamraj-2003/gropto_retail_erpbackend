"""initial schema: phase1 phase2 phase3

Revision ID: 35af731b18aa
Revises:
Create Date: 2026-09-24 18:21:37.960748

Creates every table for Phase 1 (MVP), Phase 2 (operational expansion) and Phase 3
(enterprise/omnichannel expansion) in one pass, plus extensions, the shared revision
sequence used for master-data sync watermarks, and Phase 1 seed data (roles,
permissions, role-permission grants, default loyalty config).

Tables are created via SQLAlchemy metadata (topologically sorted by FK dependency,
so this is safe against reordering as models are added), and this migration is the
one place downgrade() fully reverses — every later phase adds new revisions on top
rather than editing this one.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

import app.models  # noqa: F401 — registers every model on Base.metadata
from app.core.database import Base

revision: str = "35af731b18aa"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()

    op.execute('create extension if not exists "pgcrypto"')
    op.execute("create extension if not exists pg_trgm")
    op.execute("create sequence if not exists global_revision_seq")

    Base.metadata.create_all(bind=bind)

    op.execute("create index if not exists idx_products_name_trgm on products using gin (name gin_trgm_ops)")
    op.execute("create index if not exists idx_products_revision on products (revision)")
    op.execute("create index if not exists idx_movements_product_store on inventory_movements (product_id, store_id, created_at)")
    op.execute("create index if not exists idx_sales_store_date on sales (store_id, billed_at)")
    op.execute("create index if not exists idx_approvals_status_store on approval_requests (status, store_id)")
    op.execute("create index if not exists idx_audit_entity on audit_log (entity_type, entity_id)")

    # audit_log is physically immutable: revoke update/delete at the grant level so
    # no application code path — not even a Super Admin request — can alter it.
    op.execute("revoke update, delete on audit_log from public")

    _seed(bind)


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind)
    op.execute("drop sequence if exists global_revision_seq")


def _seed(bind) -> None:
    role_defs = [
        ("super_admin", "Super Admin", 100, 999999),
        ("admin", "Admin", 20, 5000),
        ("store_manager", "Store Manager", 15, 2000),
        ("cashier", "Cashier", 5, 200),
        ("inventory_user", "Inventory User", 0, 0),
    ]
    permission_codes = [
        "product.update", "product.deactivate",
        "inventory.adjust", "inventory.view",
        "sale.create", "sale.discount", "sale.void",
        "loyalty.configure", "loyalty.redeem",
        "approval.decide", "approval.request",
        "user.manage", "device.manage",
        "report.export", "import.commit",
        "vendor.manage", "purchase.manage",
        "config.manage",
    ]
    role_permission_map = {
        "super_admin": permission_codes,
        "admin": [
            "product.update", "inventory.adjust", "inventory.view", "sale.create", "sale.discount", "sale.void",
            "loyalty.redeem", "approval.request", "user.manage", "report.export", "import.commit",
            "vendor.manage", "purchase.manage",
        ],
        "store_manager": [
            "inventory.adjust", "inventory.view", "sale.create", "sale.discount", "sale.void",
            "loyalty.redeem", "approval.request", "report.export",
        ],
        "cashier": ["sale.create", "sale.discount", "loyalty.redeem", "inventory.view"],
        "inventory_user": ["inventory.adjust", "inventory.view", "purchase.manage"],
    }

    conn = bind
    role_ids: dict[str, str] = {}
    for code, name, pct, val in role_defs:
        result = conn.execute(
            sa.text(
                "insert into roles (code, name, max_discount_percent, max_discount_value) "
                "values (:code, :name, :pct, :val) returning id"
            ),
            {"code": code, "name": name, "pct": pct, "val": val},
        )
        role_ids[code] = result.scalar_one()

    perm_ids: dict[str, str] = {}
    for code in permission_codes:
        result = conn.execute(sa.text("insert into permissions (code) values (:code) returning id"), {"code": code})
        perm_ids[code] = result.scalar_one()

    for role_code, perms in role_permission_map.items():
        for perm_code in perms:
            conn.execute(
                sa.text("insert into role_permissions (role_id, permission_id) values (:r, :p)"),
                {"r": role_ids[role_code], "p": perm_ids[perm_code]},
            )

    conn.execute(
        sa.text(
            "insert into loyalty_config (id, earn_rate, redeem_value, min_balance_to_redeem, max_redeem_share) "
            "values (true, 0.01, 0.5, 50, 0.5)"
        )
    )
