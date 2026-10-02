from fastapi import APIRouter

from app.api.v1 import (
    ai,
    approvals,
    audit,
    auth,
    cash,
    checklist,
    rbac,
    control_tower,
    crm,
    crm_advanced,
    customers,
    dashboard,
    enterprise,
    finance_advanced,
    gift_vouchers,
    gst,
    hr,
    hr_advanced,
    imports,
    inventory,
    inventory_intelligence,
    loyalty,
    master_data,
    orders,
    payments,
    phase2_misc,
    procurement,
    procurement_advanced,
    products,
    promotions,
    purchases,
    reports,
    returns,
    stock_count,
    stores,
    sync,
    transfers,
    users,
    wms,
)

api_router = APIRouter(prefix="/api/v1")

# Phase 1
api_router.include_router(auth.router)
api_router.include_router(products.router)
api_router.include_router(inventory.router)
api_router.include_router(sync.router)
api_router.include_router(approvals.router)
api_router.include_router(purchases.router)
api_router.include_router(reports.router)
api_router.include_router(ai.router)
api_router.include_router(imports.router)
api_router.include_router(dashboard.router)
api_router.include_router(loyalty.router)
api_router.include_router(stores.router)
api_router.include_router(audit.router)
api_router.include_router(payments.router)
api_router.include_router(users.router)

# Phase 2
api_router.include_router(returns.router)
api_router.include_router(transfers.router)
api_router.include_router(cash.router)
api_router.include_router(procurement.router)
api_router.include_router(phase2_misc.router)
api_router.include_router(stock_count.router)
api_router.include_router(gift_vouchers.router)
api_router.include_router(customers.router)

# Phase 3
api_router.include_router(orders.router)
api_router.include_router(crm.router)
api_router.include_router(hr.router)
api_router.include_router(enterprise.router)

# Phase 4 - Blueprint Full Architecture
api_router.include_router(wms.router)
api_router.include_router(procurement_advanced.router)
api_router.include_router(inventory_intelligence.router)
api_router.include_router(promotions.router)
api_router.include_router(finance_advanced.router)
api_router.include_router(gst.router)
api_router.include_router(crm_advanced.router)
api_router.include_router(hr_advanced.router)
api_router.include_router(control_tower.router)
api_router.include_router(master_data.router)
api_router.include_router(checklist.router)
api_router.include_router(rbac.router)
