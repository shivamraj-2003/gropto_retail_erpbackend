from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission, require_store_access
from app.core.database import get_db
from app.schemas.schemas_phase2 import GrnCreate, PurchaseOrderCreate, RequisitionCreate
from app.services import procurement as procurement_service

router = APIRouter(prefix="/procurement", tags=["procurement"])


@router.post("/requisitions", status_code=201)
async def create_requisition(
    payload: RequisitionCreate,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("purchase.manage")),
) -> dict:
    require_store_access(payload.store_id, current)
    req = await procurement_service.create_requisition(db, current=current, payload=payload)
    await db.commit()
    return {"requisition_id": str(req.id)}


@router.post("/purchase-orders", status_code=201)
async def create_purchase_order(
    payload: PurchaseOrderCreate,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("purchase.manage")),
) -> dict:
    po = await procurement_service.create_purchase_order(db, current=current, payload=payload)
    await db.commit()
    return {"purchase_order_id": str(po.id), "status": po.status}


@router.post("/grn", status_code=201)
async def receive_grn(
    payload: GrnCreate,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("purchase.manage")),
) -> dict:
    if payload.store_id:
        require_store_access(payload.store_id, current)
    grn = await procurement_service.receive_grn(db, current=current, payload=payload)
    await db.commit()
    return {"grn_id": str(grn.id)}
