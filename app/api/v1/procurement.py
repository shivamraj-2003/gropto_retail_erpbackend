import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission, require_store_access
from app.core.database import get_db
from app.models.models_phase2 import Grn, PurchaseOrder, PurchaseRequisition
from app.schemas.schemas_phase2 import (
    GrnCreate,
    GrnOut,
    PurchaseOrderCreate,
    PurchaseOrderOut,
    RequisitionCreate,
    RequisitionOut,
)
from app.services import procurement as procurement_service

router = APIRouter(prefix="/procurement", tags=["procurement"])


@router.get("/requisitions", response_model=list[RequisitionOut])
async def list_requisitions(
    store_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("purchase.manage")),
) -> list[PurchaseRequisition]:
    stmt = select(PurchaseRequisition)
    if current.role_code != "super_admin":
        stmt = stmt.where(PurchaseRequisition.store_id.in_(current.store_ids))
    if store_id:
        require_store_access(store_id, current)
        stmt = stmt.where(PurchaseRequisition.store_id == store_id)
    result = await db.execute(stmt.order_by(PurchaseRequisition.created_at.desc()).limit(200))
    return list(result.scalars().all())


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


@router.get("/purchase-orders", response_model=list[PurchaseOrderOut])
async def list_purchase_orders(
    status_filter: str | None = None,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("purchase.manage")),
) -> list[PurchaseOrder]:
    stmt = select(PurchaseOrder)
    if status_filter:
        stmt = stmt.where(PurchaseOrder.status == status_filter)
    result = await db.execute(stmt.order_by(PurchaseOrder.created_at.desc()).limit(200))
    return list(result.scalars().all())


@router.post("/purchase-orders", status_code=201)
async def create_purchase_order(
    payload: PurchaseOrderCreate,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("purchase.manage")),
) -> dict:
    po = await procurement_service.create_purchase_order(db, current=current, payload=payload)
    await db.commit()
    return {"purchase_order_id": str(po.id), "status": po.status}


@router.get("/grn", response_model=list[GrnOut])
async def list_grns(
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("purchase.manage")),
) -> list[Grn]:
    result = await db.execute(select(Grn).order_by(Grn.created_at.desc()).limit(200))
    return list(result.scalars().all())


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
