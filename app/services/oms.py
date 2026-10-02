"""Omnichannel order management (Phase 3): one order queue, stock reservation
against available-to-promise, allocation, pick/pack with controlled
substitution, dispatch, OTP delivery confirmation, cancellation, refund, and
reverse logistics.

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
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser
from app.models.models import Customer, Product, Store, User
from app.models.models_phase3 import Order, OrderItem, OrderRefund, OrderReturn, OrderReturnItem, OrderStatusHistory
from app.schemas.schemas_phase3 import (
    OrderCreate,
    OrderDeliverRequest,
    OrderDispatchRequest,
    OrderPickRequest,
    OrderReturnCreate,
)
from app.services import payments as payments_service
from app.services import sms as sms_service
from app.services.audit import write_audit
from app.services.coupons import validate_and_apply_coupon
from app.services.inventory import adjust_damaged, adjust_reserved, apply_movement, get_available_to_promise
from app.services.loyalty import DuplicateLedgerEntry, apply_ledger_entry, get_config, get_earn_multiplier
from app.services.pricing import resolve_price
from app.services.promotion_engine import build_lines, evaluate_and_log

DELIVERY_OTP_VALIDITY_HOURS = 24
REFUND_APPROVAL_THRESHOLD = 1000.0  # mirrors services/returns.py's in-store equivalent


async def _record_status_change(
    db: AsyncSession, *, order: Order, from_status: str | None, to_status: str, changed_by: uuid.UUID | None, reason: str | None = None
) -> None:
    """Point 8 audit fix: only the current status was ever stored — this
    gives the order a real, queryable history."""
    db.add(
        OrderStatusHistory(order_id=order.id, from_status=from_status, to_status=to_status, changed_by=changed_by, reason=reason)
    )


async def _allocate_store(db: AsyncSession, *, items: list) -> uuid.UUID:
    stores = list((await db.execute(select(Store.id).where(Store.is_active.is_(True)))).scalars().all())
    if not stores:
        raise HTTPException(status_code=409, detail="No active stores exist to allocate this order to")

    workload_rows = (
        await db.execute(
            select(Order.allocated_store_id, func.count())
            .where(Order.status.notin_(["delivered", "cancelled", "refunded", "returned"]))
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
    # Point 8 audit fix: idempotent re-submission — a retried "place order"
    # call with the same key returns the existing order instead of creating
    # a second one and reserving stock twice.
    if payload.client_idempotency_key:
        existing = (
            await db.execute(select(Order).where(Order.client_idempotency_key == payload.client_idempotency_key))
        ).scalar_one_or_none()
        if existing is not None:
            return existing

    if payload.payment_mode == "prepaid" and not payload.payment_reference:
        raise HTTPException(status_code=400, detail="payment_reference is required for a prepaid order")

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

    # Point 9 audit fix: unit_price used to be taken directly from the client
    # with zero server-side validation — a malicious or buggy client could
    # place an order at any price. The server now resolves the real
    # applicable price (store/city override, else central Product price) for
    # every line and ignores whatever price the client sent.
    products = {
        p.id: p
        for p in (await db.execute(select(Product).where(Product.id.in_([i.product_id for i in payload.items])))).scalars().all()
    }
    resolved_items: list[dict] = []
    for item in payload.items:
        product = products.get(item.product_id)
        if product is None or not product.is_active:
            raise HTTPException(status_code=400, detail=f"Product {item.product_id} is not a valid active product")
        resolved_price, _mrp = await resolve_price(db, product=product, store_id=target_store_id)
        resolved_items.append({"product_id": item.product_id, "quantity": item.quantity, "unit_price": resolved_price})

    subtotal = sum(i["quantity"] * i["unit_price"] for i in resolved_items)

    lines = await build_lines(db, items=resolved_items)

    order = Order(
        channel=payload.channel,
        customer_id=customer_id,
        allocated_store_id=target_store_id,
        status="reserved",
        subtotal=subtotal,
        grand_total=subtotal,
        delivery_address=payload.delivery_address,
        payment_mode=payload.payment_mode,
        payment_reference=payload.payment_reference,
        payment_status="paid" if payload.payment_mode == "prepaid" else "cod_pending",
        client_idempotency_key=payload.client_idempotency_key,
    )
    db.add(order)
    await db.flush()

    promo_discount = await evaluate_and_log(
        db, store_id=target_store_id, lines=lines, source_type="order", source_id=order.id
    )
    coupon_discount = 0.0
    if payload.coupon_code:
        coupon_discount = await validate_and_apply_coupon(
            db,
            code=payload.coupon_code,
            customer_id=customer_id,
            cart_subtotal=float(subtotal) - promo_discount,
            source_type="order",
            source_id=order.id,
        )

    discount_total = round(promo_discount + coupon_discount, 2)
    order.discount_total = discount_total
    order.coupon_code = payload.coupon_code
    order.grand_total = float(subtotal) - discount_total

    for item in resolved_items:
        db.add(OrderItem(order_id=order.id, product_id=item["product_id"], quantity=item["quantity"], unit_price=item["unit_price"]))
        await adjust_reserved(db, product_id=item["product_id"], store_id=target_store_id, delta=item["quantity"])

    await _record_status_change(db, order=order, from_status=None, to_status="reserved", changed_by=current.user_id if current else None)
    await write_audit(
        db,
        user_id=current.user_id if current else None,
        role_code=current.role_code if current else None,
        store_id=target_store_id,
        device_id=None,
        action="order.reserved",
        entity_type="order",
        entity_id=order.id,
        new_value={
            "subtotal": float(subtotal),
            "discount_total": discount_total,
            "channel": payload.channel,
            "payment_mode": payload.payment_mode,
        },
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
    prev_status = order.status
    order.status = "picking"
    order.picking_started_at = datetime.now(timezone.utc)
    await _record_status_change(db, order=order, from_status=prev_status, to_status="picking", changed_by=current.user_id)
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=order.allocated_store_id,
        device_id=None,
        action="order.picking_started",
        entity_type="order",
        entity_id=order.id,
    )
    return order


async def pick_order(db: AsyncSession, *, current: CurrentUser, order: Order, payload: OrderPickRequest) -> Order:
    """Point 8 audit fix: picking used to jump directly to status="packed" in
    this same call (no distinct packing step — see confirm_pack below), and
    substitution was uncontrolled (any product id accepted with no
    validation, no price adjustment). Both are fixed here: this now only
    completes picking (status="picked"); substitutes are validated as real,
    active, in-stock products; and when a barcode is supplied it's checked
    against the (possibly substituted) product before the pick is accepted."""
    if order.status not in ("reserved", "allocated", "picking"):
        raise HTTPException(status_code=409, detail=f"Cannot pick order in status {order.status}")
    items_by_id = {item.id: item for item in order.items}
    substitutions: list[dict] = []
    for picked in payload.items:
        item = items_by_id.get(picked.order_item_id)
        if item is None:
            raise HTTPException(status_code=400, detail="Unknown order item")

        effective_product_id = item.product_id
        if picked.substituted_product_id is not None and picked.substituted_product_id != item.product_id:
            substitute = await db.get(Product, picked.substituted_product_id)
            if substitute is None or not substitute.is_active:
                raise HTTPException(status_code=400, detail=f"Substitute product {picked.substituted_product_id} is not a valid active product")
            atp = await get_available_to_promise(db, product_id=substitute.id, store_id=order.allocated_store_id)
            if atp < picked.picked_qty:
                raise HTTPException(status_code=409, detail=f"Substitute product {substitute.id} does not have enough available stock")
            effective_product_id = substitute.id
            substitutions.append(
                {
                    "order_item_id": str(item.id),
                    "original_product_id": str(item.product_id),
                    "substitute_product_id": str(substitute.id),
                    "price_diff": float(substitute.selling_price) - float(item.unit_price),
                }
            )

        if picked.scanned_barcode is not None:
            product = await db.get(Product, effective_product_id)
            if product is None or product.barcode != picked.scanned_barcode:
                raise HTTPException(status_code=409, detail="Scanned barcode does not match the expected (or substituted) product")

        item.picked_qty = picked.picked_qty
        item.substituted_product_id = picked.substituted_product_id

    # Caller skipped the explicit start-picking step (legacy direct-pick
    # flow) — backfill a start time so packing-duration math stays sane
    # instead of silently nulling out.
    if order.picking_started_at is None:
        order.picking_started_at = order.created_at
    prev_status = order.status
    order.status = "picked"
    order.picked_at = datetime.now(timezone.utc)
    order.picked_by = current.user_id
    await _record_status_change(db, order=order, from_status=prev_status, to_status="picked", changed_by=current.user_id)
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=order.allocated_store_id,
        device_id=None,
        action="order.picked",
        entity_type="order",
        entity_id=order.id,
        new_value={"substitutions": substitutions} if substitutions else None,
    )
    return order


async def confirm_pack(db: AsyncSession, *, current: CurrentUser, order: Order) -> Order:
    """Point 8 audit fix: packing used to be the exact same action as
    picking (no distinct step, no packer identity). This is the real,
    separate packing confirmation."""
    if order.status != "picked":
        raise HTTPException(status_code=409, detail=f"Order must be picked before it can be packed (currently {order.status})")
    prev_status = order.status
    order.status = "packed"
    order.packed_at = datetime.now(timezone.utc)
    order.packed_by = current.user_id
    await _record_status_change(db, order=order, from_status=prev_status, to_status="packed", changed_by=current.user_id)
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=order.allocated_store_id,
        device_id=None,
        action="order.packed",
        entity_type="order",
        entity_id=order.id,
    )
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
    # Point 8 audit fix: the OTP previously never expired.
    order.delivery_otp_expires_at = datetime.now(timezone.utc) + timedelta(hours=DELIVERY_OTP_VALIDITY_HOURS)
    prev_status = order.status
    order.status = "dispatched"
    order.dispatched_at = datetime.now(timezone.utc)
    await _record_status_change(db, order=order, from_status=prev_status, to_status="dispatched", changed_by=current.user_id)
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

    # Point 16 audit fix: the delivery OTP previously had to be pulled by the
    # rider via an authenticated API call (get_delivery_otp) — nothing ever
    # pushed it to the customer, so a rider at the door had no way to prove
    # the OTP to a customer who didn't already have it. Same honest
    # is_configured()-gated pattern as every other provider in this
    # codebase: a real SMS attempt, not a fabricated "sent" status.
    if order.customer_id is not None and sms_service.is_configured():
        customer = await db.get(Customer, order.customer_id)
        if customer is not None and customer.phone:
            await sms_service.send_sms(
                customer.phone,
                f"Your Gropto order is out for delivery. Share OTP {order.delivery_otp} with the rider to confirm receipt.",
            )

    return order


def _fulfilled_unit_price(item: OrderItem, substitute_prices: dict[uuid.UUID, float]) -> float:
    if item.substituted_product_id is not None:
        return substitute_prices.get(item.substituted_product_id, float(item.unit_price))
    return float(item.unit_price)


async def deliver_order(db: AsyncSession, *, current: CurrentUser, order: Order, payload: OrderDeliverRequest) -> Order:
    if order.status != "dispatched":
        raise HTTPException(status_code=409, detail="Order is not out for delivery")
    # Point 8 audit fix: previously any user holding inventory.adjust could
    # complete delivery for any order — now only the assigned rider (or an
    # enterprise-wide role, for exception handling) may confirm it.
    if current.user_id != order.rider_id and not current.sees_all_stores():
        raise HTTPException(status_code=403, detail="Only the assigned rider can confirm this delivery")
    if order.delivery_otp_expires_at is not None and datetime.now(timezone.utc) > order.delivery_otp_expires_at:
        raise HTTPException(status_code=400, detail="Delivery OTP has expired — redispatch the order to issue a new one")
    if payload.otp != order.delivery_otp:
        raise HTTPException(status_code=400, detail="Invalid delivery OTP")

    substitute_ids = {item.substituted_product_id for item in order.items if item.substituted_product_id is not None}
    substitute_prices: dict[uuid.UUID, float] = {}
    if substitute_ids:
        rows = (await db.execute(select(Product.id, Product.selling_price).where(Product.id.in_(substitute_ids)))).all()
        substitute_prices = {r.id: float(r.selling_price) for r in rows}

    # Point 8 audit fix: a short-picked or substituted order previously still
    # charged/invoiced the full originally-ordered amount — settlement is
    # now recomputed from what was actually fulfilled.
    new_subtotal = 0.0
    for item in order.items:
        actual_product_id = item.substituted_product_id or item.product_id
        qty = item.picked_qty if item.picked_qty is not None else item.quantity
        unit_price = _fulfilled_unit_price(item, substitute_prices)
        new_subtotal += float(qty) * unit_price
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

    old_grand_total = float(order.grand_total)
    delivery_fee = float(order.grand_total) - float(order.subtotal)
    order.subtotal = new_subtotal
    order.grand_total = new_subtotal + delivery_fee

    prev_status = order.status
    order.status = "delivered"
    order.delivered_at = datetime.now(timezone.utc)
    if order.payment_mode == "cod":
        order.payment_status = "paid"

    # Point 9 audit fix: OMS never earned loyalty points at all — only POS
    # did. Earned on final settled grand_total (post substitution/short-pick
    # recompute above), at delivery rather than order placement, since that's
    # when the sale is actually final here (mirrors inventory's own
    # delta-at-delivery timing for the same reason).
    if order.customer_id is not None and float(order.grand_total) > 0:
        config = await get_config(db)
        multiplier = await get_earn_multiplier(db, customer_id=order.customer_id)
        earn_points = round(float(order.grand_total) * float(config.earn_rate) * multiplier, 2)
        if earn_points > 0:
            try:
                await apply_ledger_entry(
                    db,
                    customer_id=order.customer_id,
                    delta_points=earn_points,
                    reason="earn",
                    source_type="order",
                    source_id=order.id,
                )
            except DuplicateLedgerEntry:
                pass

    await _record_status_change(db, order=order, from_status=prev_status, to_status="delivered", changed_by=current.user_id if current else None)
    await write_audit(
        db,
        user_id=current.user_id if current else None,
        role_code=current.role_code if current else None,
        store_id=order.allocated_store_id,
        device_id=None,
        action="order.delivered",
        entity_type="order",
        entity_id=order.id,
        old_value={"grand_total": old_grand_total},
        new_value={"grand_total": float(order.grand_total)},
    )
    return order


async def get_delivery_otp(*, current: CurrentUser, order: Order) -> dict:
    """Point 8 audit fix: delivery_otp used to be returned in the general
    order-detail response to anyone who could view the order. Now only the
    assigned rider (or an enterprise-wide role) can fetch it, and only once
    the order is actually dispatched."""
    if current.user_id != order.rider_id and not current.sees_all_stores():
        raise HTTPException(status_code=403, detail="Only the assigned rider can view this delivery OTP")
    if order.status != "dispatched":
        raise HTTPException(status_code=409, detail=f"Order is {order.status}, not out for delivery")
    return {"otp": order.delivery_otp, "expires_at": order.delivery_otp_expires_at}


async def initiate_refund(
    db: AsyncSession, *, current: CurrentUser, order: Order, amount: float, reason: str | None
) -> OrderRefund:
    """Point 8 audit fix: refund didn't exist anywhere in the OMS — Order had
    no payment field at all. Duplicate-refund prevention: refuses a second
    refund while one is already initiated or completed."""
    existing = (
        await db.execute(
            select(OrderRefund).where(OrderRefund.order_id == order.id, OrderRefund.status.in_(["initiated", "completed"]))
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"A refund for this order is already {existing.status}")

    if order.payment_mode != "prepaid" or order.payment_status != "paid" or not order.payment_reference:
        # Nothing was actually collected via a gateway — mark the refund
        # record as not-required rather than fabricating a gateway call.
        refund = OrderRefund(order_id=order.id, amount=amount, method="not_required", status="completed", reason=reason, created_by=current.user_id)
        db.add(refund)
        order.payment_status = "refunded"
        return refund

    if not payments_service.is_configured():
        refund = OrderRefund(order_id=order.id, amount=amount, method="manual", status="initiated", reason=reason, created_by=current.user_id)
        db.add(refund)
        order.payment_status = "refund_initiated"
        return refund

    try:
        result = payments_service.refund_payment(order.payment_reference, amount)
        refund = OrderRefund(
            order_id=order.id,
            amount=amount,
            method="razorpay",
            gateway_refund_id=result.get("id"),
            status="completed",
            reason=reason,
            created_by=current.user_id,
        )
        order.payment_status = "refunded"
    except Exception as exc:  # noqa: BLE001 — gateway failures must not crash the cancellation/return flow
        refund = OrderRefund(order_id=order.id, amount=amount, method="razorpay", status="failed", reason=f"{reason or ''} | gateway error: {exc}", created_by=current.user_id)
        order.payment_status = "refund_initiated"
    db.add(refund)
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=order.allocated_store_id,
        device_id=None,
        action="order.refund_initiated",
        entity_type="order",
        entity_id=order.id,
        new_value={"amount": amount, "status": refund.status, "method": refund.method},
        reason=reason,
    )
    return refund


async def cancel_order(db: AsyncSession, *, current: CurrentUser, order: Order, reason: str | None) -> Order:
    # Point 8 audit fix: a dispatched order (rider already has the physical
    # goods and an active OTP) used to be cancellable through this same
    # path with no recall handling at all — now requires the explicit
    # recall_dispatched_order flow instead.
    if order.status == "dispatched":
        raise HTTPException(status_code=409, detail="Order is already dispatched — use the recall endpoint instead of a plain cancel")
    if order.status in ("delivered", "cancelled", "returned", "refunded"):
        raise HTTPException(status_code=409, detail=f"Cannot cancel order in status {order.status}")
    for item in order.items:
        await adjust_reserved(db, product_id=item.product_id, store_id=order.allocated_store_id, delta=-item.quantity)
    prev_status = order.status
    order.status = "cancelled"
    await _record_status_change(db, order=order, from_status=prev_status, to_status="cancelled", changed_by=current.user_id, reason=reason)
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
    if order.payment_status == "paid":
        await initiate_refund(db, current=current, order=order, amount=float(order.grand_total), reason=reason or "Order cancelled")
    return order


async def recall_dispatched_order(db: AsyncSession, *, current: CurrentUser, order: Order, reason: str | None) -> Order:
    """Point 8 audit fix: the real missing piece — cancelling an order that's
    already out with a rider. Clears the dispatch assignment/OTP (the rider
    must physically return the goods; this just reverses the system state),
    releases the reservation, and refunds if payment was already collected."""
    if order.status != "dispatched":
        raise HTTPException(status_code=409, detail=f"Order is {order.status}, not dispatched — use the regular cancel endpoint")
    for item in order.items:
        await adjust_reserved(db, product_id=item.product_id, store_id=order.allocated_store_id, delta=-item.quantity)
    order.rider_id = None
    order.delivery_otp = None
    order.delivery_otp_expires_at = None
    prev_status = order.status
    order.status = "cancelled"
    await _record_status_change(db, order=order, from_status=prev_status, to_status="cancelled", changed_by=current.user_id, reason=reason)
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=order.allocated_store_id,
        device_id=None,
        action="order.recalled",
        entity_type="order",
        entity_id=order.id,
        new_value={"reason": reason},
    )
    if order.payment_status == "paid":
        await initiate_refund(db, current=current, order=order, amount=float(order.grand_total), reason=reason or "Dispatched order recalled")
    return order


async def create_order_return(db: AsyncSession, *, current: CurrentUser, order: Order, payload: OrderReturnCreate) -> OrderReturn:
    """Point 8 audit fix: reverse logistics for online orders didn't exist at
    all — the in-store Return model is keyed to a POS Sale, not an Order.
    Mirrors services/returns.py's validation (quantity/refund capped against
    what was actually delivered, netting out any prior return on the same line)."""
    if order.status != "delivered":
        raise HTTPException(status_code=409, detail=f"Can only return a delivered order (currently {order.status})")

    items_by_id = {item.id: item for item in order.items}
    for line in payload.items:
        item = items_by_id.get(line.order_item_id)
        if item is None or (item.product_id != line.product_id and item.substituted_product_id != line.product_id):
            raise HTTPException(status_code=400, detail=f"Order item {line.order_item_id} does not match product {line.product_id} on this order")

        delivered_qty = float(item.picked_qty) if item.picked_qty is not None else float(item.quantity)
        already_returned = (
            await db.execute(
                select(OrderReturnItem.quantity)
                .join(OrderReturn, OrderReturn.id == OrderReturnItem.return_id)
                .where(OrderReturnItem.order_item_id == line.order_item_id, OrderReturn.status != "rejected")
            )
        ).scalars().all()
        returned_so_far = sum(float(q) for q in already_returned)
        remaining = delivered_qty - returned_so_far
        if line.quantity > remaining + 0.01:
            raise HTTPException(status_code=400, detail=f"Only {remaining} units of this line remain returnable")

        unit_price = _fulfilled_unit_price(item, {})  # original fulfilled price; substitution price diff already settled at delivery
        max_refund = unit_price * line.quantity
        if line.refund_amount > max_refund + 0.01:
            raise HTTPException(status_code=400, detail=f"Refund amount {line.refund_amount} exceeds the line's value {round(max_refund, 2)}")

    refund_total = sum(line.refund_amount for line in payload.items)
    ret = OrderReturn(order_id=order.id, requested_by=current.user_id, reason=payload.reason, refund_total=refund_total, status="pending")
    db.add(ret)
    await db.flush()

    for line in payload.items:
        db.add(
            OrderReturnItem(
                return_id=ret.id,
                order_item_id=line.order_item_id,
                product_id=line.product_id,
                quantity=line.quantity,
                disposition=line.disposition,
                refund_amount=line.refund_amount,
            )
        )
        if line.disposition == "saleable":
            await apply_movement(
                db,
                product_id=line.product_id,
                store_id=order.allocated_store_id,
                delta=line.quantity,
                reason_code="order_return_to_saleable",
                source_type="order_return_item",
                source_id=ret.id,
                created_by=current.user_id,
                device_id=None,
            )
        elif line.disposition == "damaged":
            await adjust_damaged(db, product_id=line.product_id, store_id=order.allocated_store_id, delta=line.quantity)

    ret.status = "completed"
    prev_status = order.status
    order.status = "returned"
    await _record_status_change(db, order=order, from_status=prev_status, to_status="returned", changed_by=current.user_id, reason=payload.reason)
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=order.allocated_store_id,
        device_id=None,
        action="order.returned",
        entity_type="order",
        entity_id=order.id,
        new_value={"refund_total": refund_total, "line_count": len(payload.items)},
        reason=payload.reason,
    )

    if refund_total > 0 and order.payment_status == "paid":
        await initiate_refund(db, current=current, order=order, amount=refund_total, reason=f"Return: {payload.reason}" if payload.reason else "Order return")

    return ret
