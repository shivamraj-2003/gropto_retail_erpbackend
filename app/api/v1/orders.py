import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy.orm import selectinload

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

DISPATCH_SLA_MINUTES = 120


async def _get_order(db: AsyncSession, order_id: uuid.UUID) -> Order:
    stmt = select(Order).options(selectinload(Order.items)).where(Order.id == order_id)
    order = (await db.execute(stmt)).scalar_one_or_none()
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    return order


@router.get("/kpis")
async def order_kpis(
    store_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("sale.create")),
) -> dict:
    """Blueprint §8 Online KPIs: fill rate, cancellation %, item-not-found %,
    delivery TAT, pick/pack/dispatch timing — all now backed by real
    per-stage timestamps (Point 3 audit fix; start_picking()/pick_order()/
    dispatch_order() stamp picking_started_at/packed_at/dispatched_at)."""
    store_filter = "and allocated_store_id = :store_id" if store_id else ""
    params = {"store_id": str(store_id)} if store_id else {}

    orders_row = (
        await db.execute(
            text(
                f"""
                select
                    count(*) as total,
                    count(*) filter (where status = 'cancelled') as cancelled,
                    count(*) filter (where status = 'delivered') as delivered,
                    avg(extract(epoch from (delivered_at - created_at)) / 60) filter (where delivered_at is not null) as avg_delivery_minutes,
                    avg(extract(epoch from (picking_started_at - created_at)) / 60) filter (where picking_started_at is not null) as avg_pick_time_minutes,
                    avg(extract(epoch from (packed_at - picking_started_at)) / 60) filter (where packed_at is not null and picking_started_at is not null) as avg_pack_time_minutes,
                    avg(extract(epoch from (dispatched_at - packed_at)) / 60) filter (where dispatched_at is not null and packed_at is not null) as avg_dispatch_wait_minutes,
                    count(*) filter (where dispatched_at is not null and packed_at is not null
                        and dispatched_at - packed_at <= interval '{DISPATCH_SLA_MINUTES} minutes') as dispatched_within_sla,
                    count(*) filter (where dispatched_at is not null and packed_at is not null) as dispatched_with_timing
                from orders
                where date_trunc('month', created_at) = date_trunc('month', now()) {store_filter}
                """
            ),
            params,
        )
    ).one()

    items_row = (
        await db.execute(
            text(
                f"""
                select
                    count(oi.id) as total_lines,
                    count(oi.id) filter (where oi.substituted_product_id is not null or coalesce(oi.picked_qty, oi.quantity) < oi.quantity) as not_found_lines
                from order_items oi
                join orders o on o.id = oi.order_id
                where date_trunc('month', o.created_at) = date_trunc('month', now()) {store_filter}
                """
            ),
            params,
        )
    ).one()

    total = int(orders_row.total)
    fill_rate_pct = round(int(orders_row.delivered) / total * 100, 1) if total else None
    cancellation_pct = round(int(orders_row.cancelled) / total * 100, 1) if total else None
    item_not_found_pct = (
        round(int(items_row.not_found_lines) / int(items_row.total_lines) * 100, 1) if items_row.total_lines else None
    )

    dispatched_with_timing = int(orders_row.dispatched_with_timing)
    dispatch_sla_pct = round(int(orders_row.dispatched_within_sla) / dispatched_with_timing * 100, 1) if dispatched_with_timing else None

    return {
        "orders_mtd": total,
        "fill_rate_pct": fill_rate_pct,
        "cancellation_pct": cancellation_pct,
        "item_not_found_pct": item_not_found_pct,
        "avg_delivery_tat_minutes": round(float(orders_row.avg_delivery_minutes), 1) if orders_row.avg_delivery_minutes is not None else None,
        "avg_pick_time_minutes": round(float(orders_row.avg_pick_time_minutes), 1) if orders_row.avg_pick_time_minutes is not None else None,
        "avg_pack_time_minutes": round(float(orders_row.avg_pack_time_minutes), 1) if orders_row.avg_pack_time_minutes is not None else None,
        "avg_dispatch_wait_minutes": round(float(orders_row.avg_dispatch_wait_minutes), 1) if orders_row.avg_dispatch_wait_minutes is not None else None,
        "dispatch_sla_minutes_threshold": DISPATCH_SLA_MINUTES,
        "dispatch_sla_pct": dispatch_sla_pct,
    }


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


@router.post("/{order_id}/start-picking", response_model=OrderOut)
async def start_picking(
    order_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("inventory.adjust")),
) -> Order:
    order = await _get_order(db, order_id)
    order = await oms.start_picking(db, current=current, order=order)
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
