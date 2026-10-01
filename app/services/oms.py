"""Omnichannel order management (Phase 3): one order queue, stock reservation
against available-to-promise, allocation, pick/pack with substitution, dispatch,
OTP delivery confirmation, cancellation.

Allocation (blueprint §8: "serviceability, stock availability, distance,
workload and SLA"): distance is not computed — stores/customers have no
geocoordinates anywhere in the schema, and adding real distance would mean
either lat/long columns plus an external geocoding API for delivery
addresses, or a fabricated proxy, neither of which belongs in this pass.
Serviceability, stock availability and workload ARE real here: when the
caller doesn't pin a preferred_store_id, every active store is checked for
full ATP across all order lines (serviceability + availability), and the
candidate with the fewest currently-open orders wins (workload balancing).
"""

import secrets
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser
from app.models.models import Customer, Store, User
from app.models.models_phase3 import Order, OrderItem
from app.schemas.schemas_phase3 import OrderCreate, OrderDeliverRequest, OrderDispatchRequest, OrderPickRequest
from app.services.audit import write_audit
from app.services.inventory import adjust_reserved, apply_movement, get_available_to_promise


async def _allocate_store(db: AsyncSession, *, items: list) -> uuid.UUID:
    stores = list((await db.execute(select(Store.id).where(Store.is_active.is_(True)))).scalars().all())
    if not stores:
        raise HTTPException(status_code=409, detail="No active stores exist to allocate this order to")

    workload_rows = (
        await db.execute(
            select(Order.allocated_store_id, func.count())
            .where(Order.status.notin_(["delivered", "cancelled", "refunded"]))
            .group_by(Order.allocated_store_id)
        )
    ).all()
    workload = {row[0]: row[1] for row in workload_rows}

    serviceable: list[tuple[uuid.UUID, int]] = []
    for store_id in stores:
        fully_stocked = True
        for item in items:
            atp = await get_available_to_promise(db, product_id=item.product_id, store_id=store_id)
            if atp < item.quantity:
                fully_stocked = False
                break
        if fully_stocked:
            serviceable.append((store_id, workload.get(store_id, 0)))

    if not serviceable:
        raise HTTPException(status_code=409, detail="No store currently has stock to fulfil every line of this order")

    serviceable.sort(key=lambda pair: pair[1])
    return serviceable[0][0]


async def create_order(db: AsyncSession, *, current: CurrentUser, payload: OrderCreate) -> Order:
    if payload.preferred_store_id is not None:
        target_store_id = payload.preferred_store_id
        for item in payload.items:
            atp = await get_available_to_promise(db, product_id=item.product_id, store_id=target_store_id)
            if atp < item.quantity:
                raise HTTPException(
                    status_code=409,
                    detail=f"Insufficient available-to-promise stock for product {item.product_id}: have {atp}, need {item.quantity}",
                )
    else:
        target_store_id = await _allocate_store(db, items=payload.items)

    result = await db.execute(select(Customer).where(Customer.phone == payload.customer_phone))
    customer = result.scalar_one_or_none()
    if customer is None:
        customer = Customer(phone=payload.customer_phone)
        db.add(customer)
        await db.flush()
    customer_id = customer.id

    subtotal = sum(item.quantity * item.unit_price for item in payload.items)
    order = Order(
        channel=payload.channel,
        customer_id=customer_id,
        allocated_store_id=target_store_id,
        status="reserved",
        subtotal=subtotal,
        grand_total=subtotal,
        delivery_address=payload.delivery_address,
    )
    db.add(order)
    await db.flush()

    for item in payload.items:
        db.add(OrderItem(order_id=order.id, product_id=item.product_id, quantity=item.quantity, unit_price=item.unit_price))
        await adjust_reserved(db, product_id=item.product_id, store_id=target_store_id, delta=item.quantity)

    await write_audit(
        db,
        user_id=current.user_id if current else None,
        role_code=current.role_code if current else None,
        store_id=target_store_id,
        device_id=None,
        action="order.reserved",
        entity_type="order",
        entity_id=order.id,
        new_value={"subtotal": float(subtotal)},
    )
    return order


async def start_picking(db: AsyncSession, *, current: CurrentUser, order: Order) -> Order:
    """Point 3 audit fix: the blueprint's Picking Time and Packing Time are
    two distinct KPIs, but nothing marked when picking actually began — only
    order-placed and pack-complete existed. This gives picking its own start
    event so the two durations are separately measurable instead of one
    combined "pick+pack" number."""
    if order.status not in ("reserved", "allocated"):
        raise HTTPException(status_code=409, detail=f"Cannot start picking an order in status {order.status}")
    order.status = "picking"
    order.picking_started_at = datetime.now(timezone.utc)
    return order


async def pick_order(db: AsyncSession, *, current: CurrentUser, order: Order, payload: OrderPickRequest) -> Order:
    if order.status not in ("reserved", "allocated", "picking"):
        raise HTTPException(status_code=409, detail=f"Cannot pick order in status {order.status}")
    items_by_id = {item.id: item for item in order.items}
    for picked in payload.items:
        item = items_by_id.get(picked.order_item_id)
        if item is None:
            raise HTTPException(status_code=400, detail="Unknown order item")
        item.picked_qty = picked.picked_qty
        item.substituted_product_id = picked.substituted_product_id
    # Caller skipped the explicit start-picking step (legacy direct-pack
    # flow) — backfill a start time so packing-duration math stays sane
    # instead of silently nulling out.
    if order.picking_started_at is None:
        order.picking_started_at = order.created_at
    order.status = "packed"
    order.packed_at = datetime.now(timezone.utc)
    return order


async def dispatch_order(db: AsyncSession, *, current: CurrentUser, order: Order, payload: OrderDispatchRequest) -> Order:
    if order.status != "packed":
        raise HTTPException(status_code=409, detail="Order must be packed before dispatch")
    # Point 2 audit fix: rider_id used to be a bare, unvalidated UUID — any
    # value was accepted and stamped on the order with no check it referred
    # to a real person at all.
    rider = await db.get(User, payload.rider_id)
    if rider is None or not rider.is_active:
        raise HTTPException(status_code=400, detail="rider_id does not match an active user")
    order.rider_id = payload.rider_id
    order.delivery_otp = f"{secrets.randbelow(10**6):06d}"
    order.status = "dispatched"
    order.dispatched_at = datetime.now(timezone.utc)
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=order.allocated_store_id,
        device_id=None,
        action="order.dispatched",
        entity_type="order",
        entity_id=order.id,
        new_value={"rider_id": str(payload.rider_id)},
    )
    return order


async def deliver_order(db: AsyncSession, *, current: CurrentUser, order: Order, payload: OrderDeliverRequest) -> Order:
    if order.status != "dispatched":
        raise HTTPException(status_code=409, detail="Order is not out for delivery")
    if payload.otp != order.delivery_otp:
        raise HTTPException(status_code=400, detail="Invalid delivery OTP")

    for item in order.items:
        actual_product_id = item.substituted_product_id or item.product_id
        qty = item.picked_qty if item.picked_qty is not None else item.quantity
        await adjust_reserved(db, product_id=item.product_id, store_id=order.allocated_store_id, delta=-item.quantity)
        await apply_movement(
            db,
            product_id=actual_product_id,
            store_id=order.allocated_store_id,
            delta=-qty,
            reason_code="online_sale",
            source_type="order_item",
            source_id=item.id,
            created_by=current.user_id if current else None,
            device_id=None,
        )

    order.status = "delivered"
    order.delivered_at = datetime.now(timezone.utc)
    await write_audit(
        db,
        user_id=current.user_id if current else None,
        role_code=current.role_code if current else None,
        store_id=order.allocated_store_id,
        device_id=None,
        action="order.delivered",
        entity_type="order",
        entity_id=order.id,
    )
    return order


async def cancel_order(db: AsyncSession, *, current: CurrentUser, order: Order, reason: str | None) -> Order:
    if order.status in ("delivered", "cancelled"):
        raise HTTPException(status_code=409, detail=f"Cannot cancel order in status {order.status}")
    for item in order.items:
        await adjust_reserved(db, product_id=item.product_id, store_id=order.allocated_store_id, delta=-item.quantity)
    order.status = "cancelled"
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=order.allocated_store_id,
        device_id=None,
        action="order.cancelled",
        entity_type="order",
        entity_id=order.id,
        new_value={"reason": reason},
    )
    return order
