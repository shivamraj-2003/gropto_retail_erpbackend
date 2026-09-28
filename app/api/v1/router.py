from fastapi import APIRouter

from app.api.v1 import (
    ai,
    approvals,
    audit,
    auth,
    cash,
    crm,
    dashboard,
    enterprise,
    hr,
    imports,
    inventory,
    loyalty,
    orders,
    payments,
    phase2_misc,
    procurement,
    products,
    purchases,
    reports,
    returns,
    stores,
    sync,
    transfers,
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

# Phase 2
api_router.include_router(returns.router)
api_router.include_router(transfers.router)
api_router.include_router(cash.router)
api_router.include_router(procurement.router)
api_router.include_router(phase2_misc.router)

# Phase 3
api_router.include_router(orders.router)
api_router.include_router(crm.router)
api_router.include_router(hr.router)
api_router.include_router(enterprise.router)
