"""Point 14: the single source of truth for every permission code in the ERP.

Codes are `module.feature.action`. Every protected endpoint names exactly one
of these in its `require_permission(...)` gate, and the migration that seeds
the `permissions` table reads this module — so the catalogue the RBAC admin
screens show, the codes the API enforces, and the rows in the database can't
drift apart.

`CATALOG_LEGACY` maps each code to the pre-Point-14 coarse codes (e.g.
`inventory.adjust`) whose holders must keep it. It was derived from the
endpoints themselves: if a role could call an endpoint before, it holds that
endpoint's new code after the migration — nobody silently loses access. Two
pseudo-legacy markers stand in for gates that used to be role-name checks
rather than permissions: `@admin` (the old require_admin_or_super),
`@shift_bypass` (the old cash.SHIFT_OWNERSHIP_BYPASS_ROLES) and `@force_match`
(the old purchase_head/finance_head check on vendor-invoice force-match).
"""

from fnmatch import fnmatchcase

MODULE_LABELS: dict[str, str] = {
    "ai": "AI Insights",
    "alerts": "CEO Alerts",
    "approval": "Approval Management",
    "attendance": "Attendance",
    "audit": "Audit",
    "catalog": "Product Catalogue",
    "crm": "CRM / Customer 360",
    "dashboard": "CEO Dashboard",
    "device": "Device / POS Management",
    "finance": "Finance & Accounting",
    "fraud": "Fraud / Loss Prevention",
    "gst": "GST / Tax",
    "hr": "HR",
    "import": "Data Import",
    "inventory": "Inventory",
    "loyalty": "Loyalty",
    "masterdata": "Master Data",
    "oms": "OMS / Online Orders",
    "payroll": "Payroll",
    "pos": "POS",
    "pricing": "Pricing",
    "procurement": "Procurement / Purchase Orders",
    "promotion": "Promotions",
    "rbac": "Role & Permission Management",
    "receiving": "Receiving / GRN",
    "reports": "Reports",
    "store": "Store ERP",
    "transfer": "Transfers",
    "user": "User Management",
    "vendor": "Vendor Management",
    "wms": "WMS",
    "workforce": "Workforce",
}

ACTION_LABELS: dict[str, str] = {
    "view": "View", "create": "Create", "update": "Update", "delete": "Delete/Deactivate",
    "approve": "Approve", "reject": "Reject", "cancel": "Cancel", "void": "Void/Recall",
    "refund": "Refund", "export": "Export", "import": "Import", "assign": "Assign",
    "configure": "Configure", "adjust": "Adjust", "transfer": "Transfer/Dispatch",
    "override": "Override", "reconcile": "Reconcile", "close": "Close/Resolve",
}

CATALOG_LEGACY: dict[str, tuple[str, ...]] = {
    'ai.insight.view': ('report.export',),
    'alerts.ceo_alert.assign': ('fraud.manage',),
    'alerts.ceo_alert.close': ('fraud.manage',),
    'alerts.ceo_alert.view': ('fraud.view',),
    'approval.history.view': ('approval.decide',),
    'approval.request.approve': ('approval.decide',),
    'approval.request.reject': ('approval.decide',),
    'approval.request.view': ('approval.decide',),
    'approval.rule.configure': (),
    'approval.rule.view': ('approval.decide',),
    'attendance.record.update': ('hr.manage',),
    'attendance.record.view': ('hr.manage',),
    'audit.log.export': ('audit.view',),
    'audit.log.view': ('audit.view',),
    'catalog.product.create': ('product.update',),
    'catalog.product.delete': ('product.deactivate',),
    'catalog.product.view': ('inventory.view',),
    'crm.analytics.update': ('crm.view',),
    'crm.analytics.view': ('crm.view',),
    'crm.audience.create': ('crm.campaign',),
    'crm.audience.view': ('crm.view',),
    'crm.campaign.approve': ('crm.campaign',),
    'crm.campaign.create': ('crm.campaign',),
    'crm.campaign.view': ('report.export',),
    'crm.consent.update': ('crm.consent',),
    'crm.customer.view': ('crm.view',),
    'crm.ticket.create': ('crm.view',),
    'crm.ticket.update': ('crm.view',),
    'crm.ticket.view': ('crm.view',),
    'dashboard.ceo.view': ('report.export',),
    'dashboard.drilldown.view': ('report.export',),
    'dashboard.exception.view': ('report.export',),
    'dashboard.store.view': ('report.export',),
    'device.config.configure': ('config.manage',),
    'device.config.view': ('device.manage',),
    'device.credential.view': ('inventory.view',),
    'device.health.view': ('device.manage',),
    'device.terminal.create': ('device.manage',),
    'device.terminal.delete': ('device.manage',),
    'device.terminal.view': ('device.manage',),
    'finance.accounting.export': ('report.export',),
    'finance.bank_deposit.create': ('purchase.manage',),
    'finance.bank_deposit.view': ('report.export',),
    'finance.budget.create': ('purchase.manage',),
    'finance.budget.update': ('report.export',),
    'finance.budget.view': ('report.export',),
    'finance.expense.create': ('purchase.manage',),
    'finance.expense.view': ('report.export',),
    'finance.payable.create': ('purchase.manage',),
    'finance.payable.view': ('report.export',),
    'finance.pnl.view': ('report.export',),
    'finance.receivable.view': ('report.export',),
    'finance.reconciliation.reconcile': ('report.export',),
    'fraud.alert.assign': ('fraud.manage',),
    'fraud.alert.close': ('fraud.manage',),
    'fraud.alert.create': ('fraud.manage',),
    'fraud.alert.view': ('fraud.view',),
    'fraud.suspicious_billing.close': ('fraud.manage',),
    'fraud.suspicious_billing.create': ('fraud.manage',),
    'fraud.suspicious_billing.view': ('fraud.view',),
    'gst.applicability.view': ('report.export',),
    'gst.einvoice.cancel': ('finance.gst_configure',),
    'gst.einvoice.create': ('finance.gst_configure',),
    'gst.einvoice.view': ('report.export',),
    'hr.document.update': ('hr.manage',),
    'hr.document.view': ('hr.manage',),
    'hr.employee.create': ('hr.manage',),
    'hr.employee.update': ('hr.manage',),
    'hr.employee.view': ('hr.manage',),
    'hr.leave.approve': ('hr.manage',),
    'hr.leave.create': ('hr.manage',),
    'hr.leave.view': ('hr.manage',),
    'hr.transfer.create': ('hr.manage',),
    'hr.transfer.view': ('hr.manage',),
    'import.batch.view': ('import.commit',),
    'import.opening_stock.import': ('import.commit',),
    'import.product.import': ('import.commit',),
    'inventory.block.create': ('inventory.adjust',),
    'inventory.block.delete': ('inventory.adjust',),
    'inventory.damage.create': ('inventory.adjust',),
    'inventory.intelligence.update': ('inventory.adjust',),
    'inventory.intelligence.view': ('inventory.view',),
    'inventory.reorder.configure': ('inventory.adjust',),
    'inventory.replenishment.update': ('inventory.adjust',),
    'inventory.replenishment.view': ('inventory.view',),
    'inventory.stock.adjust': ('inventory.adjust',),
    'inventory.stock.reconcile': ('device.manage',),
    'inventory.stock.view': ('inventory.view',),
    'inventory.stock_count.close': ('inventory.adjust',),
    'inventory.stock_count.create': ('inventory.view',),
    'inventory.stock_count.update': ('inventory.view',),
    'inventory.stock_count.view': ('inventory.view',),
    'loyalty.config.configure': ('loyalty.configure',),
    'loyalty.config.view': ('inventory.view',),
    'loyalty.tier.create': ('loyalty.configure',),
    'loyalty.tier.view': ('inventory.view',),
    'loyalty.wallet.adjust': ('loyalty.configure',),
    'loyalty.wallet.view': ('inventory.view',),
    'masterdata.chart_of_account.create': ('config.manage',),
    'masterdata.chart_of_account.update': ('config.manage',),
    'masterdata.chart_of_account.view': ('report.export',),
    'masterdata.cluster.create': ('config.manage',),
    'masterdata.cluster.update': ('config.manage',),
    'masterdata.cluster.view': ('inventory.view',),
    'masterdata.company.create': ('finance.gst_configure',),
    'masterdata.company.update': ('finance.gst_configure',),
    'masterdata.company.view': ('inventory.view',),
    'masterdata.department.create': ('config.manage',),
    'masterdata.department.update': ('config.manage',),
    'masterdata.department.view': ('inventory.view',),
    'masterdata.payment_mode.create': ('config.manage',),
    'masterdata.payment_mode.update': ('config.manage',),
    'masterdata.payment_mode.view': ('inventory.view',),
    'masterdata.reason_code.create': ('config.manage',),
    'masterdata.reason_code.update': ('config.manage',),
    'masterdata.reason_code.view': ('inventory.view',),
    'masterdata.region.create': ('config.manage',),
    'masterdata.region.update': ('config.manage',),
    'masterdata.region.view': ('inventory.view',),
    'masterdata.store.create': ('@admin',),
    'masterdata.store.update': ('@admin',),
    'masterdata.store.view': ('inventory.view',),
    'masterdata.warehouse.create': ('config.manage',),
    'masterdata.warehouse.update': ('config.manage',),
    'masterdata.warehouse.view': ('inventory.view',),
    'oms.delivery.close': ('inventory.adjust',),
    'oms.delivery.transfer': ('inventory.adjust',),
    'oms.delivery.view': ('inventory.adjust',),
    'oms.fulfilment.update': ('inventory.adjust',),
    'oms.order.cancel': ('sale.void',),
    'oms.order.create': ('sale.create',),
    'oms.order.view': ('sale.create',),
    'oms.order.void': ('sale.void',),
    'oms.refund.refund': ('sale.void',),
    'oms.refund.view': ('sale.void',),
    'payroll.adjustment.create': ('hr.manage',),
    'payroll.adjustment.view': ('hr.manage',),
    'payroll.summary.view': ('hr.manage',),
    'pos.cash_movement.create': ('sale.create',),
    'pos.customer.view': ('sale.create',),
    'pos.gift_voucher.create': ('sale.create',),
    'pos.gift_voucher.view': ('sale.create',),
    'pos.payment.create': ('sale.create',),
    'pos.payment.view': ('sale.create',),
    'pos.return.refund': ('sale.void',),
    'pos.return.update': ('sale.void',),
    'pos.return.view': ('sale.void',),
    'pos.sale.create': ('sale.create',),
    'pos.shift.close': ('sale.create',),
    'pos.shift.create': ('sale.create',),
    'pos.shift.override': ('@shift_bypass',),
    'pos.shift.view': ('sale.create',),
    'pricing.discount.create': ('loyalty.configure',),
    'pricing.discount.update': ('loyalty.configure',),
    'pricing.discount.view': ('inventory.view',),
    'pricing.price.override': ('loyalty.configure',),
    'pricing.price.update': ('product.update',),
    'pricing.price.view': ('inventory.view',),
    'pricing.scheduled_price.create': ('loyalty.configure',),
    'pricing.scheduled_price.view': ('loyalty.configure',),
    'procurement.forecast.view': ('purchase.manage',),
    'procurement.purchase.create': ('purchase.manage',),
    'procurement.purchase.view': ('purchase.manage',),
    'procurement.purchase_order.cancel': ('purchase.manage',),
    'procurement.purchase_order.create': ('purchase.manage',),
    'procurement.purchase_order.view': ('purchase.manage',),
    'procurement.requisition.create': ('purchase.manage',),
    'procurement.requisition.view': ('purchase.manage',),
    'procurement.rfq.create': ('purchase.manage',),
    'procurement.rfq.view': ('purchase.manage',),
    'procurement.vendor_invoice.create': ('purchase.manage',),
    'procurement.vendor_invoice.override': ('@force_match',),
    'procurement.vendor_invoice.view': ('purchase.manage',),
    'promotion.analytics.view': ('report.export',),
    'promotion.coupon.create': ('loyalty.configure',),
    'promotion.coupon.update': ('loyalty.configure',),
    'promotion.coupon.view': ('inventory.view',),
    'promotion.rule.create': ('loyalty.configure',),
    'promotion.rule.update': ('loyalty.configure',),
    'promotion.rule.view': ('inventory.view',),
    'rbac.audit.view': ('audit.view',),
    'rbac.permission.assign': (),
    'rbac.permission.view': ('user.manage',),
    'rbac.role.create': (),
    'rbac.role.delete': (),
    'rbac.role.update': (),
    'rbac.role.view': ('inventory.view',),
    'rbac.scope.assign': (),
    'rbac.user_role.assign': (),
    'receiving.grn.create': ('purchase.manage',),
    'receiving.grn.view': ('purchase.manage',),
    'reports.customer.export': ('report.export',),
    'reports.finance.export': ('report.export',),
    'reports.insight.view': ('report.export',),
    'reports.inventory.export': ('report.export',),
    'reports.operations.export': ('report.export',),
    'reports.purchase.export': ('report.export',),
    'reports.sales.export': ('report.export',),
    'store.checklist.update': ('inventory.adjust',),
    'store.checklist.view': ('inventory.view',),
    'store.day_close.close': ('report.export',),
    'store.footfall.update': ('inventory.adjust',),
    'transfer.discrepancy.reconcile': ('inventory.adjust',),
    'transfer.transfer.create': ('inventory.adjust',),
    'transfer.transfer.update': ('inventory.adjust',),
    'transfer.transfer.view': ('inventory.adjust',),
    'user.password.update': ('user.manage',),
    'user.user.create': ('user.manage',),
    'user.user.update': ('user.manage',),
    'user.user.view': ('user.manage',),
    'vendor.note.create': ('purchase.manage',),
    'vendor.note.view': ('purchase.manage',),
    'vendor.performance.view': ('purchase.manage',),
    'vendor.vendor.create': ('vendor.manage',),
    'vendor.vendor.update': ('vendor.manage',),
    'vendor.vendor.view': ('purchase.manage',),
    'wms.batch.view': ('inventory.view',),
    'wms.dispatch.transfer': ('inventory.adjust',),
    'wms.indent.create': ('inventory.adjust',),
    'wms.indent.transfer': ('inventory.adjust',),
    'wms.indent.view': ('inventory.view',),
    'wms.location.view': ('inventory.view',),
    'wms.pack.update': ('inventory.adjust',),
    'wms.pick.create': ('inventory.adjust',),
    'wms.pick.update': ('inventory.adjust',),
    'wms.pick.view': ('inventory.view',),
    'wms.putaway.create': ('inventory.adjust',),
    'wms.putaway.update': ('inventory.adjust',),
    'wms.putaway.view': ('inventory.view',),
    'workforce.productivity.view': ('hr.manage',),
    'workforce.shift.create': ('hr.manage',),
    'workforce.shift.view': ('hr.manage',),
}

#: Every catalogue code, sorted.
ALL_CODES: tuple[str, ...] = tuple(sorted(CATALOG_LEGACY))


def split_code(code: str) -> tuple[str, str, str]:
    module, feature, action = code.split(".")
    return module, feature, action


def describe(code: str) -> str:
    module, feature, action = split_code(code)
    return f"{ACTION_LABELS.get(action, action.title())} {feature.replace('_', ' ')} ({MODULE_LABELS.get(module, module)})"


# Blueprint section 14 "typical access", granted on top of whatever each role
# already held (CATALOG_LEGACY). Patterns are fnmatch globs over catalogue
# codes; SENSITIVE_CODES are never matched by a wildcard and must be named.
SENSITIVE_CODES = frozenset({
    "device.credential.view",
    "rbac.role.create", "rbac.role.update", "rbac.role.delete",
    "rbac.permission.assign", "approval.rule.configure",
})

_APPROVER = ["approval.request.view", "approval.request.approve", "approval.request.reject", "approval.history.view"]

ROLE_BLUEPRINT: dict[str, list[str]] = {
    "ceo": [
        "dashboard.*", "alerts.ceo_alert.*", "reports.*", "finance.*.view", "finance.accounting.export",
        "*.analytics.view", "inventory.intelligence.view", "vendor.performance.view", "procurement.forecast.view",
        "payroll.summary.view", "workforce.productivity.view", "fraud.*.view", "audit.log.*", "ai.insight.view",
        "approval.rule.view", *_APPROVER,
    ],
    "coo": [
        "dashboard.*", "alerts.ceo_alert.*", "store.*", "inventory.*", "wms.*", "oms.*", "transfer.*",
        "receiving.*", "masterdata.store.view", "masterdata.warehouse.view", "reports.sales.export",
        "reports.inventory.export", "reports.operations.export", "reports.insight.view", "device.health.view",
        "workforce.*", "attendance.record.view", "fraud.*.view", *_APPROVER,
    ],
    "finance_head": [
        "finance.*", "gst.*", "masterdata.chart_of_account.*", "masterdata.payment_mode.view",
        "masterdata.company.view", "procurement.vendor_invoice.*", "procurement.purchase_order.view",
        "receiving.grn.view", "vendor.vendor.view", "payroll.*", "reports.finance.export",
        "reports.purchase.export", "reports.sales.export", "reports.insight.view", "dashboard.ceo.view",
        "dashboard.store.view", "audit.log.*", *_APPROVER,
    ],
    "purchase_head": [
        "vendor.*", "procurement.*", "receiving.grn.view", "pricing.price.view", "pricing.scheduled_price.view",
        "catalog.product.view", "inventory.stock.view", "inventory.replenishment.view",
        "inventory.intelligence.view", "reports.purchase.export", "reports.inventory.export",
        "dashboard.ceo.view", *_APPROVER,
    ],
    "regional_manager": [
        "dashboard.store.view", "dashboard.exception.view", "dashboard.drilldown.view", "alerts.ceo_alert.view",
        "alerts.ceo_alert.assign", "store.*", "inventory.stock.view", "inventory.intelligence.view",
        "inventory.replenishment.view", "transfer.transfer.view", "oms.order.view", "fraud.*.view",
        "reports.sales.export", "reports.inventory.export", "reports.operations.export", "reports.insight.view",
        "attendance.record.view", "workforce.shift.view", "hr.employee.view",
    ],
    "store_manager": [
        "store.*", "dashboard.store.view", "pos.*", "inventory.stock.view", "inventory.stock_count.*",
        "inventory.replenishment.*", "hr.employee.view", "hr.leave.view", "hr.leave.approve",
        "attendance.record.*", "workforce.shift.*",
    ],
    "cashier": [
        "pos.sale.create", "pos.shift.view", "pos.shift.create", "pos.shift.close", "pos.customer.view",
        "pos.payment.*", "pos.gift_voucher.*", "catalog.product.view", "inventory.stock.view",
    ],
    # "Warehouse User" in the blueprint; the role code predates it.
    "inventory_user": [
        "wms.*", "receiving.grn.*", "transfer.transfer.*", "inventory.stock.view", "inventory.stock_count.*",
        "catalog.product.view", "masterdata.warehouse.view", "oms.order.view", "oms.fulfilment.update",
        "oms.delivery.transfer",
    ],
    "system_admin": [
        "user.*", "rbac.role.view", "rbac.permission.view", "rbac.user_role.assign", "rbac.scope.assign",
        "rbac.audit.view", "masterdata.*", "device.terminal.*", "device.config.*", "device.health.view",
        "audit.log.*", "approval.rule.view",
    ],
}

#: Roles that see every store unless narrowed by a company scope — seeds
#: roles.scope_level = 'global' (Regional Manager is deliberately 'assigned').
GLOBAL_SCOPE_ROLES = ("super_admin", "admin", "system_admin", "ceo", "coo", "finance_head", "purchase_head")

# Blueprint section 14: "System Admin — configuration and user administration;
# no silent transaction editing". Anything that writes or decides a business
# transaction can never be granted to the system_admin role.
TRANSACTIONAL_MODULES = frozenset({
    "pos", "oms", "inventory", "wms", "transfer", "receiving", "procurement", "finance", "gst", "payroll", "store",
})
_TRANSACTIONAL_EXTRA = frozenset({
    "loyalty.wallet.adjust", "pricing.price.override", "approval.request.approve", "approval.request.reject",
})


def is_transaction_edit(code: str) -> bool:
    module, _feature, action = split_code(code)
    if code in _TRANSACTIONAL_EXTRA:
        return True
    return module in TRANSACTIONAL_MODULES and action not in ("view", "export")


def expand_patterns(patterns: list[str]) -> set[str]:
    out: set[str] = set()
    for pattern in patterns:
        if "*" not in pattern:
            if pattern in CATALOG_LEGACY:
                out.add(pattern)
            continue
        out.update(c for c in ALL_CODES if c not in SENSITIVE_CODES and fnmatchcase(c, pattern))
    return out
