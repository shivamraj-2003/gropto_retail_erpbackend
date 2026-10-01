import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy.orm import selectinload

from app.api.deps import CurrentUser, require_permission, require_store_access
from app.core.database import get_db
from app.models.models_phase2 import Grn, PurchaseOrder, PurchaseRequisition
from app.schemas.schemas import Page
from app.schemas.schemas_phase2 import (
    GrnCreate,
    GrnOut,
    PurchaseOrderCancelIn,
    PurchaseOrderCreate,
    PurchaseOrderOut,
    RequisitionCreate,
    RequisitionOut,
)
from app.services import procurement as procurement_service

router = APIRouter(prefix="/procurement", tags=["procurement"])


@router.get("/requisitions", response_model=Page[RequisitionOut])
async def list_requisitions(
    store_id: uuid.UUID | None = None,
    limit: int = 20,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("purchase.manage")),
) -> Page[RequisitionOut]:
    stmt = select(PurchaseRequisition).options(selectinload(PurchaseRequisition.items))
    if not current.sees_all_stores():
        stmt = stmt.where(PurchaseRequisition.store_id.in_(current.store_ids))
    if store_id:
        require_store_access(store_id, current)
        stmt = stmt.where(PurchaseRequisition.store_id == store_id)
    capped_limit = min(limit, 200)
    total = (await db.execute(select(func.count()).select_from(select(PurchaseRequisition.id).subquery()))).scalar_one()
    result = await db.execute(stmt.order_by(PurchaseRequisition.created_at.desc()).limit(capped_limit).offset(offset))
    return Page(items=list(result.scalars().all()), total=total, limit=capped_limit, offset=offset)


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


@router.get("/purchase-orders", response_model=Page[PurchaseOrderOut])
async def list_purchase_orders(
    status_filter: str | None = None,
    limit: int = 20,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("purchase.manage")),
) -> Page[PurchaseOrderOut]:
    stmt = select(PurchaseOrder).options(selectinload(PurchaseOrder.items))
    if status_filter:
        stmt = stmt.where(PurchaseOrder.status == status_filter)
    capped_limit = min(limit, 200)
    total = (await db.execute(select(func.count()).select_from(select(PurchaseOrder.id).subquery()))).scalar_one()
    result = await db.execute(stmt.order_by(PurchaseOrder.created_at.desc()).limit(capped_limit).offset(offset))
    return Page(items=list(result.scalars().all()), total=total, limit=capped_limit, offset=offset)


@router.post("/purchase-orders", status_code=201)
async def create_purchase_order(
    payload: PurchaseOrderCreate,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("purchase.manage")),
) -> dict:
    po = await procurement_service.create_purchase_order(db, current=current, payload=payload)
    await db.commit()
    return {"purchase_order_id": str(po.id), "status": po.status}


@router.post("/purchase-orders/{po_id}/cancel", response_model=PurchaseOrderOut)
async def cancel_purchase_order(
    po_id: uuid.UUID,
    payload: PurchaseOrderCancelIn,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("purchase.manage")),
) -> PurchaseOrder:
    po = await db.get(PurchaseOrder, po_id)
    if po is None:
        raise HTTPException(status_code=404, detail="Purchase order not found")
    po = await procurement_service.cancel_purchase_order(db, current=current, po=po, reason=payload.reason)
    await db.commit()
    await db.refresh(po)
    return po


@router.get("/grn", response_model=Page[GrnOut])
async def list_grns(
    limit: int = 20,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("purchase.manage")),
) -> Page[GrnOut]:
    stmt = select(Grn)
    capped_limit = min(limit, 200)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    result = await db.execute(stmt.order_by(Grn.created_at.desc()).limit(capped_limit).offset(offset))
    return Page(items=list(result.scalars().all()), total=total, limit=capped_limit, offset=offset)


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
    return {"grn_id": str(grn.id), "grn_number": grn.grn_number}
