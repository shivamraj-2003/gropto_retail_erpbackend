"""point14_enterprise_rbac

Revision ID: a8b9c0d1e2f4
Revises: e6f7a8b9c0d1
Create Date: 2026-10-02 10:00:00.000000

Point 14 — enterprise RBAC & approval matrix, built on the existing
roles/permissions/role_permissions tables rather than beside them:

* roles gain description/is_active/is_system/scope_level; permissions gain
  module/feature/action/description/is_deprecated; users gain is_super_admin;
  approval_requests gain required_levels/approved_levels.
* New user_roles (multiple roles), user_scopes (company/region/cluster/city/
  warehouse/department), approval_rules (the matrix), approval_steps (history).
* Seeds the full module.feature.action catalogue (app/core/permission_catalog.py).
* Access-preserving grant migration: every role that held a pre-Point-14
  coarse code receives every new code whose endpoints that coarse code used
  to gate. Then the blueprint section-14 role grants are layered on top
  (never removing anything), except that System Admin never receives a
  transaction-editing permission.
* Old coarse codes and their grants are kept (flagged is_deprecated) — no
  existing row is deleted.
* Approval rules are seeded with exactly the thresholds that were hardcoded
  in the services, so behaviour is unchanged until someone edits the matrix.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.core.permission_catalog import (
    ALL_CODES,
    CATALOG_LEGACY,
    GLOBAL_SCOPE_ROLES,
    ROLE_BLUEPRINT,
    describe,
    expand_patterns,
    is_transaction_edit,
    split_code,
)

revision: str = "a8b9c0d1e2f4"
down_revision: Union[str, None] = "e6f7a8b9c0d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ROLE_DESCRIPTIONS = {
    "super_admin": "Full authority over every module, store and setting.",
    "admin": "Franchise administrator; sensitive changes queue for Super Admin approval.",
    "ceo": "Company-wide dashboards, strategic reports and high-level approvals.",
    "coo": "Stores, warehouses, fulfilment and operational approvals.",
    "finance_head": "Finance, reconciliation, payables, tax and financial approvals.",
    "purchase_head": "Vendors, purchase orders, rates and procurement analytics.",
    "regional_manager": "Assigned stores and their operational exceptions.",
    "store_manager": "Own store inventory, staff, store closing and controlled approvals.",
    "cashier": "POS and permitted customer transactions only.",
    "inventory_user": "Assigned receiving, picking and dispatch.",
    "system_admin": "System configuration and user administration; no transaction editing.",
}

# Threshold rules mirror the constants previously hardcoded in the services
# (cash.py, finance.py, hr_adjustments.py, returns.py, vendor_payments.py,
# inventory.py, stock_count.py, transfers.py, approvals.py).
# (request_type, name, threshold, min_amount, approver_role_codes)
APPROVAL_RULES = [
    ("purchase_order_approval", "Purchase order", None, None, None),
    ("purchase_order_approval", "High-value purchase order (> 50,000)", None, 50000, ["purchase_head", "finance_head"]),
    ("cash_movement_approval", "Cash pickup / payout", 2000, None, None),
    ("cash_variance_escalation", "Cash variance at shift close", None, None, None),
    ("expense_approval", "Store expense", 5000, None, None),
    ("hr_adjustment_approval", "Payroll adjustment", 5000, None, None),
    ("return_approval", "Refund / return", 1000, None, None),
    ("vendor_payment_approval", "Vendor payment", 20000, None, None),
    ("high_stock_adjustment", "Stock adjustment (units)", 50, None, None),
    ("stock_count_adjustment", "Stock count variance (units)", 50, None, None),
    ("transfer_discrepancy_resolution", "Transfer discrepancy (units)", 20, None, None),
    ("price_change", "Product price change", None, None, None),
    ("scheduled_price_change", "Scheduled price change", None, None, None),
    ("product_deactivation", "Product deactivation", None, None, None),
    ("high_discount", "High discount", None, None, None),
    ("loyalty_rule_change", "Loyalty / discount rule change", None, None, None),
    ("config_change", "Configuration / master-data import", None, None, None),
    ("store_create", "New store", None, None, None),
    ("store_update", "Store change", None, None, None),
    ("user_create", "New user", None, None, None),
    ("user_permission_change", "User role / store change", None, None, None),
]


def upgrade() -> None:
    bind = op.get_bind()

    op.add_column("roles", sa.Column("description", sa.Text()))
    op.add_column("roles", sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False))
    op.add_column("roles", sa.Column("is_system", sa.Boolean(), server_default=sa.text("false"), nullable=False))
    op.add_column("roles", sa.Column("scope_level", sa.String(), server_default="assigned", nullable=False))
    op.add_column("roles", sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()))
    op.add_column("roles", sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()))

    op.add_column("permissions", sa.Column("module", sa.String()))
    op.add_column("permissions", sa.Column("feature", sa.String()))
    op.add_column("permissions", sa.Column("action", sa.String()))
    op.add_column("permissions", sa.Column("description", sa.Text()))
    op.add_column("permissions", sa.Column("is_deprecated", sa.Boolean(), server_default=sa.text("false"), nullable=False))

    op.add_column("users", sa.Column("is_super_admin", sa.Boolean(), server_default=sa.text("false"), nullable=False))

    op.add_column("approval_requests", sa.Column("required_levels", sa.Integer(), server_default="1", nullable=False))
    op.add_column("approval_requests", sa.Column("approved_levels", sa.Integer(), server_default="0", nullable=False))

    op.create_table(
        "user_roles",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("role_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("roles.id"), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_table(
        "user_scopes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("scope_type", sa.String(), nullable=False),
        sa.Column("scope_id", postgresql.UUID(as_uuid=True)),
        sa.Column("scope_value", sa.String()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint(
            "scope_type in ('company','region','cluster','city','warehouse','department')", name="ck_user_scopes_type"
        ),
    )
    op.create_index("ix_user_scopes_user_id", "user_scopes", ["user_id"])
    op.create_index(
        "uq_user_scopes_entry", "user_scopes", ["user_id", "scope_type", sa.text("coalesce(scope_id::text, scope_value)")],
        unique=True,
    )
    op.create_table(
        "approval_rules",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("request_type", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("threshold_amount", sa.Numeric(14, 2)),
        sa.Column("min_amount", sa.Numeric(14, 2)),
        sa.Column("max_amount", sa.Numeric(14, 2)),
        sa.Column("approver_permission", sa.String(), server_default="approval.request.approve", nullable=False),
        sa.Column("approver_role_codes", postgresql.ARRAY(sa.String())),
        sa.Column("levels", sa.Integer(), server_default="1", nullable=False),
        sa.Column("maker_checker", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("conditions", postgresql.JSONB()),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint("levels between 1 and 5", name="ck_approval_rules_levels"),
    )
    op.create_index("ix_approval_rules_request_type", "approval_rules", ["request_type"])
    op.create_table(
        "approval_steps",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("approval_requests.id"), nullable=False),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.Column("approver_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("approver_role_code", sa.String()),
        sa.Column("decision", sa.String(), nullable=False),
        sa.Column("note", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_approval_steps_request_id", "approval_steps", ["request_id"])
    op.create_index("uq_approval_steps_request_approver", "approval_steps", ["request_id", "approver_id"], unique=True)

    # --- permission catalogue -------------------------------------------------
    legacy_codes = set(bind.execute(sa.text("select code from permissions")).scalars().all())
    for code in legacy_codes - set(ALL_CODES):
        bind.execute(
            sa.text(
                "update permissions set is_deprecated = true, module = split_part(code, '.', 1), "
                "description = 'Legacy coarse permission (pre-Point-14); superseded by the module catalogue' "
                "where code = :c"
            ),
            {"c": code},
        )
    for code in ALL_CODES:
        module, feature, action = split_code(code)
        bind.execute(
            sa.text(
                "insert into permissions (code, module, feature, action, description) "
                "values (:c, :m, :f, :a, :d) on conflict (code) do update set "
                "module = excluded.module, feature = excluded.feature, action = excluded.action, "
                "description = excluded.description, is_deprecated = false"
            ),
            {"c": code, "m": module, "f": feature, "a": action, "d": describe(code)},
        )
    perm_ids = dict(bind.execute(sa.text("select code, id from permissions")).all())

    # --- roles ----------------------------------------------------------------
    roles = dict(bind.execute(sa.text("select code, id from roles")).all())
    bind.execute(sa.text("update roles set is_system = true"))
    bind.execute(
        sa.text("update roles set scope_level = 'global' where code = any(:codes)"), {"codes": list(GLOBAL_SCOPE_ROLES)}
    )
    bind.execute(sa.text("update roles set name = 'Warehouse User' where code = 'inventory_user' and name = 'Inventory User'"))
    for code, text in ROLE_DESCRIPTIONS.items():
        bind.execute(sa.text("update roles set description = :d where code = :c and description is null"), {"d": text, "c": code})

    held: dict[str, set[str]] = {code: set() for code in roles}
    for role_code, perm_code in bind.execute(
        sa.text(
            "select r.code, p.code from role_permissions rp join roles r on r.id = rp.role_id "
            "join permissions p on p.id = rp.permission_id"
        )
    ).all():
        held[role_code].add(perm_code)
    if "admin" in held:
        held["admin"].add("@admin")
    for code in (*GLOBAL_SCOPE_ROLES, "store_manager", "regional_manager"):
        if code in held:
            held[code].add("@shift_bypass")
    for code in ("purchase_head", "finance_head"):
        if code in held:
            held[code].add("@force_match")

    for role_code, role_id in roles.items():
        if role_code == "super_admin":
            grants = set(ALL_CODES)
        else:
            grants = {code for code, legacy in CATALOG_LEGACY.items() if held[role_code] & set(legacy)}
            grants |= expand_patterns(ROLE_BLUEPRINT.get(role_code, []))
            if role_code == "system_admin":
                grants = {g for g in grants if not is_transaction_edit(g)}
        for code in grants:
            bind.execute(
                sa.text("insert into role_permissions (role_id, permission_id) values (:r, :p) on conflict do nothing"),
                {"r": role_id, "p": perm_ids[code]},
            )

    bind.execute(
        sa.text("update users set is_super_admin = true where role_id = (select id from roles where code = 'super_admin')")
    )

    # --- approval matrix --------------------------------------------------------
    for request_type, name, threshold, min_amount, approver_roles in APPROVAL_RULES:
        bind.execute(
            sa.text(
                "insert into approval_rules (request_type, name, threshold_amount, min_amount, approver_role_codes) "
                "values (:t, :n, :th, :mn, :ar)"
            ),
            {"t": request_type, "n": name, "th": threshold, "mn": min_amount, "ar": approver_roles},
        )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "delete from role_permissions where permission_id in "
            "(select id from permissions where module is not null and is_deprecated = false)"
        )
    )
    bind.execute(sa.text("delete from permissions where module is not null and is_deprecated = false"))
    op.drop_table("approval_steps")
    op.drop_table("approval_rules")
    op.drop_table("user_scopes")
    op.drop_table("user_roles")
    op.drop_column("approval_requests", "approved_levels")
    op.drop_column("approval_requests", "required_levels")
    op.drop_column("users", "is_super_admin")
    for col in ("is_deprecated", "description", "action", "feature", "module"):
        op.drop_column("permissions", col)
    for col in ("updated_at", "created_at", "scope_level", "is_system", "is_active", "description"):
        op.drop_column("roles", col)
