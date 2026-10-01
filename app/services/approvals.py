"""Generic approval engine: one engine, not one per module.

Sensitive changes are either applied directly (Super Admin) or serialised into an
approval_requests row with the business table left untouched. On approval, a handler
registry (one function per request_type) replays the change with the approver's
authority and writes the audit row in the same transaction.

Approvals re-validate on apply: if the underlying record has moved since the request
was created, the handler marks the request 'stale' instead of silently applying an
outdated value.
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser
from app.models.models import ApprovalRequest, Product
from app.services.audit import write_audit

HandlerFn = Callable[[AsyncSession, ApprovalRequest], Awaitable[None]]
_HANDLERS: dict[str, HandlerFn] = {}


def register_handler(request_type: str):
    def wrapper(fn: HandlerFn) -> HandlerFn:
        _HANDLERS[request_type] = fn
        return fn

    return wrapper


async def submit_or_apply(
    db: AsyncSession,
    *,
    current: CurrentUser,
    request_type: str,
    entity_type: str,
    entity_id: uuid.UUID | None,
    old_value: dict | None,
    new_value: dict,
    reason: str | None,
    store_id: uuid.UUID | None,
) -> ApprovalRequest:
    request = ApprovalRequest(
        request_type=request_type,
        entity_type=entity_type,
        entity_id=entity_id,
        old_value=old_value,
        new_value=new_value,
        reason=reason,
        requested_by=current.user_id,
        store_id=store_id,
        status="pending",
    )
    db.add(request)
    await db.flush()

    if current.role_code == "super_admin":
        await _apply(db, request, approver_id=current.user_id)
    else:
        await write_audit(
            db,
            user_id=current.user_id,
            role_code=current.role_code,
            store_id=store_id,
            device_id=current.device_id,
            action=f"{request_type}.requested",
            entity_type=entity_type,
            entity_id=entity_id,
            new_value=new_value,
            approval_id=request.id,
            reason=reason,
        )
    return request


async def decide(
    db: AsyncSession, *, request_id: uuid.UUID, approver: CurrentUser, approve: bool, note: str | None
) -> ApprovalRequest:
    request = await db.get(ApprovalRequest, request_id)
    if request is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Approval request not found")
    if request.status != "pending":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Request already decided")

    request.reviewed_by = approver.user_id
    request.review_note = note
    request.reviewed_at = datetime.now(timezone.utc)

    if approve:
        await _apply(db, request, approver_id=approver.user_id)
    else:
        request.status = "rejected"
        await write_audit(
            db,
            user_id=approver.user_id,
            role_code=approver.role_code,
            store_id=request.store_id,
            device_id=None,
            action=f"{request.request_type}.rejected",
            entity_type=request.entity_type,
            entity_id=request.entity_id,
            approval_id=request.id,
            reason=note,
        )
    return request


async def _apply(db: AsyncSession, request: ApprovalRequest, *, approver_id: uuid.UUID) -> None:
    handler = _HANDLERS.get(request.request_type)
    if handler is None:
        raise HTTPException(status_code=500, detail=f"No handler registered for {request.request_type}")
    await handler(db, request)
    if request.status == "pending":
        request.status = "approved"
    await write_audit(
        db,
        user_id=approver_id,
        role_code=None,
        store_id=request.store_id,
        device_id=None,
        action=f"{request.request_type}.applied",
        entity_type=request.entity_type,
        entity_id=request.entity_id,
        old_value=request.old_value,
        new_value=request.new_value,
        approval_id=request.id,
        reason=request.reason,
    )


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


@register_handler("price_change")
async def _handle_price_change(db: AsyncSession, request: ApprovalRequest) -> None:
    product = await db.get(Product, request.entity_id)
    if product is None:
        request.status = "stale"
        return
    expected_price = request.old_value.get("selling_price") if request.old_value else None
    if expected_price is not None and float(product.selling_price) != float(expected_price):
        request.status = "stale"
        return
    if "selling_price" in request.new_value:
        product.selling_price = request.new_value["selling_price"]
    if "mrp" in request.new_value:
        product.mrp = request.new_value["mrp"]
    product.revision = product.revision + 1 if product.revision else 1


@register_handler("product_deactivation")
async def _handle_product_deactivation(db: AsyncSession, request: ApprovalRequest) -> None:
    product = await db.get(Product, request.entity_id)
    if product is None:
        request.status = "stale"
        return
    product.is_active = False


@register_handler("high_discount")
async def _handle_high_discount_noop(db: AsyncSession, request: ApprovalRequest) -> None:
    """High discounts on live bills use a manager-override PIN at the counter (no
    approval round-trip — the customer is standing there). This handler exists for
    the case where a discount RULE itself (not a single sale) is being approved."""
    from app.models.models import DiscountRule

    rule_id = request.entity_id
    if rule_id is None:
        return
    rule = await db.get(DiscountRule, rule_id)
    if rule is None:
        request.status = "stale"
        return
    for field in ("percent", "flat_amount", "active"):
        if field in request.new_value:
            setattr(rule, field, request.new_value[field])
    rule.revision = rule.revision + 1 if rule.revision else 1


@register_handler("loyalty_rule_change")
async def _handle_loyalty_rule_change(db: AsyncSession, request: ApprovalRequest) -> None:
    from app.models.models import LoyaltyConfig

    config = await db.get(LoyaltyConfig, True)
    for field in ("earn_rate", "redeem_value", "min_balance_to_redeem", "max_redeem_share"):
        if field in request.new_value:
            setattr(config, field, request.new_value[field])


@register_handler("high_stock_adjustment")
async def _handle_high_stock_adjustment(db: AsyncSession, request: ApprovalRequest) -> None:
    from app.services.inventory import apply_movement

    payload = request.new_value
    await apply_movement(
        db,
        product_id=uuid.UUID(payload["product_id"]),
        store_id=uuid.UUID(payload["store_id"]),
        delta=payload["delta"],
        reason_code=payload.get("reason_code", "adjustment"),
        source_type="approval",
        source_id=request.id,
        created_by=request.requested_by,
        device_id=None,
    )


@register_handler("cash_movement_approval")
async def _handle_cash_movement_approval(db: AsyncSession, request: ApprovalRequest) -> None:
    """Point 4 audit fix: cash-in/out had no approval step at all — any user
    with sale.create could move cash with no review. Large movements now
    queue here (same Super-Admin-applies-immediately rule as everything
    else) and only actually hit the CashMovement table on approval."""
    from app.models.models_phase2 import CashierShift, CashMovement

    payload = request.new_value
    shift = await db.get(CashierShift, uuid.UUID(payload["shift_id"]))
    if shift is None or shift.status != "open":
        request.status = "stale"
        return
    db.add(CashMovement(shift_id=shift.id, direction=payload["direction"], amount=payload["amount"], reason=payload["reason"]))


@register_handler("transfer_discrepancy_resolution")
async def _handle_transfer_discrepancy_resolution(db: AsyncSession, request: ApprovalRequest) -> None:
    from app.models.models_phase2 import Transfer

    payload = request.new_value
    transfer = await db.get(Transfer, uuid.UUID(payload["transfer_id"]))
    if transfer is None or transfer.status != "discrepancy":
        request.status = "stale"
        return
    transfer.status = "resolved"


@register_handler("stock_count_adjustment")
async def _handle_stock_count_adjustment(db: AsyncSession, request: ApprovalRequest) -> None:
    """Applies every variant line of a finalized stock count as a real
    inventory movement, once approved."""
    from app.services.inventory import apply_movement

    payload = request.new_value
    for line in payload["lines"]:
        if float(line["variance"]) == 0:
            continue
        await apply_movement(
            db,
            product_id=uuid.UUID(line["product_id"]),
            store_id=uuid.UUID(payload["store_id"]),
            delta=float(line["variance"]),
            reason_code="stock_count_adjustment",
            source_type="stock_count_line",
            source_id=uuid.UUID(line["line_id"]),
            created_by=request.requested_by,
            device_id=None,
        )


@register_handler("user_permission_change")
async def _handle_user_permission_change(db: AsyncSession, request: ApprovalRequest) -> None:
    from app.models.models import Role, User, UserStore

    user = await db.get(User, request.entity_id)
    if user is None:
        request.status = "stale"
        return
    if "role_code" in request.new_value:
        result = await db.execute(select(Role).where(Role.code == request.new_value["role_code"]))
        role = result.scalar_one_or_none()
        if role is None:
            raise HTTPException(status_code=400, detail="Unknown role_code")
        user.role_id = role.id
    if "store_ids" in request.new_value:
        await db.execute(UserStore.__table__.delete().where(UserStore.user_id == user.id))
        for store_id in request.new_value["store_ids"]:
            db.add(UserStore(user_id=user.id, store_id=uuid.UUID(store_id)))


@register_handler("user_create")
async def _handle_user_create(db: AsyncSession, request: ApprovalRequest) -> None:
    """Applies for both paths: a Super Admin's create-user call goes through
    submit_or_apply -> _apply immediately (this handler runs once, right
    away); an Admin's call queues here and this same handler runs only once
    a Super Admin approves it. The new user's id is generated up front by
    the endpoint and carried as both entity_id and new_value['user_id'] so
    it's stable across the pending window."""
    from app.models.models import Role, User, UserStore

    payload = request.new_value
    email = payload.get("email")
    existing = await db.execute(select(User).where(User.email == email))
    if existing.scalar_one_or_none() is not None:
        request.status = "rejected"
        return

    result = await db.execute(select(Role).where(Role.code == payload["role_code"]))
    role = result.scalar_one_or_none()
    if role is None:
        raise HTTPException(status_code=400, detail="Unknown role_code")

    user = User(
        id=uuid.UUID(payload["user_id"]),
        email=email,
        phone=payload.get("phone"),
        full_name=payload["full_name"],
        password_hash=payload["password_hash"],
        role_id=role.id,
        is_active=True,
    )
    db.add(user)
    await db.flush()
    for store_id in payload.get("store_ids", []):
        db.add(UserStore(user_id=user.id, store_id=uuid.UUID(store_id)))


@register_handler("store_create")
async def _handle_store_create(db: AsyncSession, request: ApprovalRequest) -> None:
    """Same shape as user_create: Super Admin's call applies right away,
    Admin's queues and this handler runs once approved. Rechecks the code
    isn't taken at apply time too, not just at submission — closes the race
    where two requests for the same code are both still pending."""
    from app.models.models import Store

    payload = request.new_value
    existing = await db.execute(select(Store).where(Store.code == payload["code"]))
    if existing.scalar_one_or_none() is not None:
        request.status = "rejected"
        return
    store = Store(
        id=uuid.UUID(payload["store_id"]),
        code=payload["code"],
        name=payload["name"],
        city=payload.get("city"),
        cluster=payload.get("cluster"),
        area_sqft=payload.get("area_sqft"),
        target_revenue_monthly=payload.get("target_revenue_monthly"),
    )
    db.add(store)


@register_handler("store_update")
async def _handle_store_update(db: AsyncSession, request: ApprovalRequest) -> None:
    from app.models.models import Store

    store = await db.get(Store, request.entity_id)
    if store is None:
        request.status = "stale"
        return
    for field in ("name", "city", "cluster", "area_sqft", "target_revenue_monthly"):
        if field in request.new_value:
            setattr(store, field, request.new_value[field])


@register_handler("config_change")
async def _handle_config_change_noop(db: AsyncSession, request: ApprovalRequest) -> None:
    """Generic configuration changes are recorded via audit only in Phase 1;
    specific config tables get their own handler as they're introduced."""
    return


# ---------------------------------------------------------------------------
# Phase 2 handlers
# ---------------------------------------------------------------------------


@register_handler("return_approval")
async def _handle_return_approval(db: AsyncSession, request: ApprovalRequest) -> None:
    from app.models.models_phase2 import Return
    from app.services.returns import finalize_return

    ret = await db.get(Return, request.entity_id)
    if ret is None or ret.status != "pending":
        request.status = "stale"
        return
    await finalize_return(db, ret)


@register_handler("purchase_order_approval")
async def _handle_purchase_order_approval(db: AsyncSession, request: ApprovalRequest) -> None:
    from app.models.models_phase2 import PurchaseOrder

    po = await db.get(PurchaseOrder, request.entity_id)
    if po is None or po.status != "pending_approval":
        request.status = "stale"
        return
    po.status = "approved"


@register_handler("expense_approval")
async def _handle_expense_approval(db: AsyncSession, request: ApprovalRequest) -> None:
    from app.models.models_phase2 import Expense

    expense = await db.get(Expense, request.entity_id)
    if expense is None or expense.status != "pending":
        request.status = "stale"
        return
    expense.status = "approved"
