"""Returns & refunds (Phase 2). A return raised against the original bill, with
line-level disposition. Small/recent returns finalize immediately; large or old
returns route through the approval engine, same pattern as everything else."""

from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser
from app.models.models import Payment, Sale, SaleItem, User
from app.models.models_phase2 import Return, ReturnItem
from app.schemas.schemas_phase2 import ReturnCreate
from app.services import email as email_service
from app.services.approvals import approval_threshold, submit_or_apply
from app.services.audit import write_audit
from app.services.inventory import adjust_damaged, apply_movement
from app.services.loyalty import DuplicateLedgerEntry, apply_ledger_entry
from app.services.wallet import DuplicateWalletEntry, credit_wallet

REFUND_APPROVAL_THRESHOLD = 1000.0
AGE_APPROVAL_THRESHOLD_DAYS = 30
REFUND_TOLERANCE = 0.01  # paise rounding slack when comparing against the original line


async def notify_requester(db: AsyncSession, *, ret: Return, decision: str) -> None:
    """Point 16 audit fix: a return's requester previously had no way to
    learn their approval decision except by reopening the Returns screen —
    honest is_configured()-gated email, same pattern as every other
    provider in this codebase."""
    if not email_service.is_configured():
        return
    requester = await db.get(User, ret.requested_by)
    if requester is None or not requester.email:
        return
    await email_service.send_email(
        requester.email,
        f"Return {decision}",
        f"<p>Your return request for sale {ret.sale_id} (₹{float(ret.refund_total):.2f}) was {decision}.</p>",
    )


async def create_return(db: AsyncSession, *, current: CurrentUser, payload: ReturnCreate) -> Return:
    sale = await db.get(Sale, payload.sale_id)
    if sale is None:
        raise HTTPException(status_code=404, detail="Original sale not found")

    # Point 4 audit fix: quantity and refund_amount used to be taken on faith
    # from the request — a caller could return more units than were ever
    # sold, or claim a refund larger than the line was worth. Both are now
    # validated against the real SaleItem, netting out quantity already
    # returned against it previously (so the same line can't be over-returned
    # across multiple separate return requests either).
    for item in payload.items:
        sale_item = await db.get(SaleItem, item.sale_item_id)
        if sale_item is None or sale_item.sale_id != payload.sale_id:
            raise HTTPException(status_code=400, detail=f"Sale item {item.sale_item_id} does not belong to sale {payload.sale_id}")

        already_returned = (
            await db.execute(
                select(ReturnItem.quantity)
                .join(Return, Return.id == ReturnItem.return_id)
                .where(ReturnItem.sale_item_id == item.sale_item_id, Return.status != "rejected")
            )
        ).scalars().all()
        returned_so_far = sum(float(q) for q in already_returned)
        remaining_returnable = float(sale_item.quantity) - returned_so_far
        if item.quantity > remaining_returnable + REFUND_TOLERANCE:
            raise HTTPException(
                status_code=400,
                detail=f"Only {remaining_returnable} units of this line remain returnable (sold {float(sale_item.quantity)}, already returned {returned_so_far})",
            )

        unit_net_price = (float(sale_item.line_total) / float(sale_item.quantity)) if float(sale_item.quantity) else 0.0
        max_refund = unit_net_price * item.quantity
        if item.refund_amount > max_refund + REFUND_TOLERANCE:
            raise HTTPException(
                status_code=400,
                detail=f"Refund amount {item.refund_amount} exceeds the original line value {round(max_refund, 2)} for this quantity",
            )

    refund_total = sum(item.refund_amount for item in payload.items)
    ret = Return(
        sale_id=payload.sale_id,
        store_id=payload.store_id,
        requested_by=current.user_id,
        reason=payload.reason,
        refund_total=refund_total,
        is_exchange=payload.is_exchange,
        status="pending",
    )
    db.add(ret)
    await db.flush()
    for item in payload.items:
        db.add(
            ReturnItem(
                return_id=ret.id,
                sale_item_id=item.sale_item_id,
                product_id=item.product_id,
                quantity=item.quantity,
                disposition=item.disposition,
                refund_amount=item.refund_amount,
            )
        )
    await db.flush()

    sale_age_days = 0
    if sale.billed_at is not None:
        billed_at = sale.billed_at
        if billed_at.tzinfo is None:
            billed_at = billed_at.replace(tzinfo=timezone.utc)
        sale_age_days = (datetime.now(timezone.utc) - billed_at).days

    needs_approval = (
        refund_total > await approval_threshold(db, "return_approval", REFUND_APPROVAL_THRESHOLD) or sale_age_days > AGE_APPROVAL_THRESHOLD_DAYS
    ) and not current.is_super_admin

    if needs_approval:
        await submit_or_apply(
            db,
            current=current,
            request_type="return_approval",
            entity_type="return",
            entity_id=ret.id,
            old_value=None,
            new_value={"refund_total": refund_total, "sale_id": str(payload.sale_id)},
            reason=payload.reason,
            store_id=payload.store_id,
        )
    else:
        await finalize_return(db, ret)
        await write_audit(
            db,
            user_id=current.user_id,
            role_code=current.role_code,
            store_id=payload.store_id,
            device_id=current.device_id,
            action="return.completed",
            entity_type="return",
            entity_id=ret.id,
            new_value={"refund_total": refund_total},
        )
    return ret


async def finalize_return(db: AsyncSession, ret: Return) -> None:
    """Applies stock disposition and marks the return completed. Called either
    directly (small/recent returns) or by the return_approval handler on approval."""
    result = await db.execute(select(ReturnItem).where(ReturnItem.return_id == ret.id))
    items = result.scalars().all()
    for item in items:
        if item.disposition == "saleable":
            await apply_movement(
                db,
                product_id=item.product_id,
                store_id=ret.store_id,
                delta=item.quantity,
                reason_code="return_to_saleable",
                source_type="return_item",
                source_id=item.id,
                created_by=ret.requested_by,
                device_id=None,
            )
        elif item.disposition == "damaged":
            # Point 7 audit fix: a damaged return previously vanished from
            # the books entirely (never credited back anywhere) — the
            # physical unit is real and should be visible as damaged stock,
            # not silently absent.
            await adjust_damaged(db, product_id=item.product_id, store_id=ret.store_id, delta=item.quantity)
        # vendor_return: genuinely leaves the store's books (goes back to the
        # vendor) — correctly not credited to any local balance.
    ret.status = "completed"

    # Point 9 audit fix: a return previously left the loyalty points earned
    # (and any points redeemed to pay for the bill) untouched — a customer
    # could return a bill, keep the cash refund, and keep the loyalty/wallet
    # side-effects the original sale generated. Reversed proportionally to
    # how much of the bill this return actually refunds.
    sale = await db.get(Sale, ret.sale_id)
    if sale is not None and sale.customer_id is not None and float(sale.grand_total) > 0:
        proportion = min(float(ret.refund_total) / float(sale.grand_total), 1.0)

        if float(sale.loyalty_points_earned) > 0:
            reversal = round(float(sale.loyalty_points_earned) * proportion, 2)
            if reversal > 0:
                try:
                    await apply_ledger_entry(
                        db,
                        customer_id=sale.customer_id,
                        delta_points=-reversal,
                        reason="return_reversal_earn",
                        source_type="return",
                        source_id=ret.id,
                    )
                except DuplicateLedgerEntry:
                    pass

        if float(sale.loyalty_points_redeemed) > 0:
            restored = round(float(sale.loyalty_points_redeemed) * proportion, 2)
            if restored > 0:
                try:
                    await apply_ledger_entry(
                        db,
                        customer_id=sale.customer_id,
                        delta_points=restored,
                        reason="return_reversal_redeem",
                        source_type="return",
                        source_id=ret.id,
                    )
                except DuplicateLedgerEntry:
                    pass

        wallet_payment = (
            await db.execute(
                select(Payment.amount).where(Payment.sale_id == sale.id, Payment.mode == "wallet")
            )
        ).scalars().first()
        if wallet_payment is not None:
            credit_amount = round(float(wallet_payment) * proportion, 2)
            if credit_amount > 0:
                try:
                    await credit_wallet(
                        db,
                        customer_id=sale.customer_id,
                        amount=credit_amount,
                        reference_type="return_refund",
                        source_type="return",
                        source_id=ret.id,
                    )
                except DuplicateWalletEntry:
                    pass


async def link_exchange(db: AsyncSession, *, current: CurrentUser, ret: Return, exchange_sale_id) -> Return:
    """Point 4 audit fix: no Exchange workflow existed at all — modeled as a
    Return flagged is_exchange, linked here to the replacement Sale once it's
    billed (the POS bills the new sale first, same as any other sale, then
    this call records which return it was in exchange for)."""
    if not ret.is_exchange:
        raise HTTPException(status_code=400, detail="This return was not marked as an exchange")
    if ret.status != "completed":
        raise HTTPException(status_code=409, detail="Return must be completed before linking its exchange sale")
    replacement = await db.get(Sale, exchange_sale_id)
    if replacement is None:
        raise HTTPException(status_code=404, detail="Replacement sale not found")
    ret.exchange_sale_id = exchange_sale_id
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=ret.store_id,
        device_id=current.device_id,
        action="return.exchange_linked",
        entity_type="return",
        entity_id=ret.id,
        new_value={"exchange_sale_id": str(exchange_sale_id)},
    )
    return ret
