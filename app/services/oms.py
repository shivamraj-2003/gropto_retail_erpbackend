"""Omnichannel order management (Phase 3): one order queue, stock reservation
against available-to-promise, allocation, pick/pack with substitution, dispatch,
OTP delivery confirmation, cancellation. Allocation here is deliberately simple
(caller-specified preferred store, checked for ATP) — multi-store allocation by
distance/workload is a further refinement on the same structure, not a rewrite.
"""

import secrets
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser
from app.models.models import Customer
from app.models.models_phase3 import Order, OrderItem
from app.schemas.schemas_phase3 import OrderCreate, OrderDeliverRequest, OrderDispatchRequest, OrderPickRequest
from app.services.audit import write_audit
from app.services.inventory import adjust_reserved, apply_movement, get_available_to_promise


async def create_order(db: AsyncSession, *, current: CurrentUser, payload: OrderCreate) -> Order:
    for item in payload.items:
        atp = await get_available_to_promise(db, product_id=item.product_id, store_id=payload.preferred_store_id)
        if atp < item.quantity:
            raise HTTPException(
                status_code=409,
                detail=f"Insufficient available-to-promise stock for product {item.product_id}: have {atp}, need {item.quantity}",
            )

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
        allocated_store_id=payload.preferred_store_id,
        status="reserved",
        subtotal=subtotal,
        grand_total=subtotal,
        delivery_address=payload.delivery_address,
    )
    db.add(order)
    await db.flush()

    for item in payload.items:
        db.add(OrderItem(order_id=order.id, product_id=item.product_id, quantity=item.quantity, unit_price=item.unit_price))
        await adjust_reserved(db, product_id=item.product_id, store_id=payload.preferred_store_id, delta=item.quantity)

    await write_audit(
        db,
        user_id=current.user_id if current else None,
        role_code=current.role_code if current else None,
        store_id=payload.preferred_store_id,
        device_id=None,
        action="order.reserved",
        entity_type="order",
        entity_id=order.id,
        new_value={"subtotal": float(subtotal)},
    )
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
    order.status = "packed"
    return order


async def dispatch_order(db: AsyncSession, *, current: CurrentUser, order: Order, payload: OrderDispatchRequest) -> Order:
    if order.status != "packed":
        raise HTTPException(status_code=409, detail="Order must be packed before dispatch")
    order.rider_id = payload.rider_id
    order.delivery_otp = f"{secrets.randbelow(10**6):06d}"
    order.status = "dispatched"
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
