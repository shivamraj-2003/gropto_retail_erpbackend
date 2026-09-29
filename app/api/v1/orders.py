import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission
from app.core.database import get_db
from app.models.models_phase3 import Order
from app.schemas.schemas import Page
from app.schemas.schemas_phase3 import (
    OrderCreate,
    OrderDeliverRequest,
    OrderDetailOut,
    OrderDispatchRequest,
    OrderOut,
    OrderPickRequest,
)
from app.services import oms

router = APIRouter(prefix="/orders", tags=["orders"])


async def _get_order(db: AsyncSession, order_id: uuid.UUID) -> Order:
    order = await db.get(Order, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    return order


@router.get("", response_model=Page[OrderOut])
async def list_orders(
    status_filter: str | None = None,
    store_id: uuid.UUID | None = None,
    limit: int = 20,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("sale.create")),
) -> Page[OrderOut]:
    """The order queue: every online order, optionally filtered by status/store —
    the packer/dispatcher screen's main list."""
    stmt = select(Order)
    if status_filter:
        stmt = stmt.where(Order.status == status_filter)
    if store_id:
        stmt = stmt.where(Order.allocated_store_id == store_id)
    capped_limit = min(limit, 200)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    result = await db.execute(stmt.order_by(Order.created_at.desc()).limit(capped_limit).offset(offset))
    return Page(items=list(result.scalars().all()), total=total, limit=capped_limit, offset=offset)


@router.get("/{order_id}", response_model=OrderDetailOut)
async def get_order(
    order_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("sale.create")),
) -> Order:
    return await _get_order(db, order_id)


@router.post("", response_model=OrderOut, status_code=201)
async def create_order(
    payload: OrderCreate,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("sale.create")),
) -> Order:
    order = await oms.create_order(db, current=current, payload=payload)
    await db.commit()
    await db.refresh(order)
    return order


@router.post("/{order_id}/pick", response_model=OrderOut)
async def pick_order(
    order_id: uuid.UUID,
    payload: OrderPickRequest,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("inventory.adjust")),
) -> Order:
    order = await _get_order(db, order_id)
    order = await oms.pick_order(db, current=current, order=order, payload=payload)
    await db.commit()
    await db.refresh(order)
    return order


@router.post("/{order_id}/dispatch", response_model=OrderOut)
async def dispatch_order(
    order_id: uuid.UUID,
    payload: OrderDispatchRequest,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("inventory.adjust")),
) -> Order:
    order = await _get_order(db, order_id)
    order = await oms.dispatch_order(db, current=current, order=order, payload=payload)
    await db.commit()
    await db.refresh(order)
    return order


@router.post("/{order_id}/deliver", response_model=OrderOut)
async def deliver_order(
    order_id: uuid.UUID,
    payload: OrderDeliverRequest,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("inventory.adjust")),
) -> Order:
    order = await _get_order(db, order_id)
    order = await oms.deliver_order(db, current=current, order=order, payload=payload)
    await db.commit()
    await db.refresh(order)
    return order


@router.post("/{order_id}/cancel", response_model=OrderOut)
async def cancel_order(
    order_id: uuid.UUID,
    reason: str | None = None,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("sale.void")),
) -> Order:
    order = await _get_order(db, order_id)
    order = await oms.cancel_order(db, current=current, order=order, reason=reason)
    await db.commit()
    await db.refresh(order)
    return order
