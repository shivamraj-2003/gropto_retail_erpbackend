import uuid
from datetime import timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission, require_store_access
from app.core.database import get_db
from app.models.models import Payment, Sale, SaleItem
from app.models.models_credit import CreditEntry
from app.models.models_paytm import PaytmPosPayment
from app.models.models_phase2 import CashierShift, DayClose
from app.services.audit import write_audit
from app.services.inventory import apply_movement
from app.services.loyalty import DuplicateLedgerEntry, apply_ledger_entry
from app.services.wallet import DuplicateWalletEntry, credit_wallet

router = APIRouter(prefix="/sales", tags=["sales"])

# Only these three are plain "money taken at the counter". Wallet, gift voucher and loyalty move balances elsewhere,
# so they are never switched to or from here.
SWITCHABLE = ("cash", "card", "upi")


class PaymentModeChange(BaseModel):
    from_mode: str
    to_mode: str
    amount: float = Field(gt=0)  # picks the payment line when a bill has more than one
    reason: str = Field(min_length=5, max_length=300)


@router.post("/{sale_id}/change-payment-mode")
async def change_payment_mode(
    sale_id: uuid.UUID,
    payload: PaymentModeChange,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("pos.shift.override")),
) -> dict:
    """Fix how a bill was paid (a bill rung up as cash that was really UPI). A manager's action: a reason is required and
    the change is written to the audit log. Refused when the money was confirmed by the Paytm machine, when the till's shift
    has closed or the store's day is closed (the count has been signed off), and for wallet, gift voucher or loyalty payments."""
    sale = await db.get(Sale, sale_id)
    if sale is None or sale.status != "completed":
        raise HTTPException(status_code=404, detail="Bill not found")
    require_store_access(sale.store_id, current)
    if payload.from_mode not in SWITCHABLE or payload.to_mode not in SWITCHABLE:
        raise HTTPException(status_code=422, detail="Only cash, card and UPI can be changed into each other")
    if payload.from_mode == payload.to_mode:
        raise HTTPException(status_code=422, detail="That is already the payment mode")

    payments = (await db.execute(select(Payment).where(Payment.sale_id == sale_id))).scalars().all()
    line = next((p for p in payments if p.mode == payload.from_mode and abs(float(p.amount) - payload.amount) < 0.01), None)
    if line is None:
        raise HTTPException(status_code=404, detail=f"This bill has no {payload.from_mode} payment of {payload.amount:g}")

    confirmed = await db.scalar(
        select(PaytmPosPayment.id).where(PaytmPosPayment.merchant_transaction_id == (line.reference or ""), PaytmPosPayment.status == "success").limit(1)
    )
    if confirmed is not None:
        raise HTTPException(status_code=409, detail="The Paytm machine confirmed this payment, so its mode cannot be changed. Refund it on the machine instead")

    billed = sale.billed_at if sale.billed_at.tzinfo else sale.billed_at.replace(tzinfo=timezone.utc)
    closed_shift = await db.scalar(
        select(CashierShift.id).where(
            CashierShift.device_id == sale.device_id, CashierShift.status == "closed",
            CashierShift.opened_at <= billed, CashierShift.closed_at >= billed,
        ).limit(1)
    )
    if closed_shift is not None:
        raise HTTPException(status_code=409, detail="That till's shift is already closed and counted. Reopen the shift first (emergency) if this must change")
    day_closed = await db.scalar(select(DayClose.id).where(DayClose.store_id == sale.store_id, DayClose.business_date == billed.date()).limit(1))
    if day_closed is not None:
        raise HTTPException(status_code=409, detail="The store's day is closed, so this bill can no longer be changed")

    line.mode = payload.to_mode
    # a bill with no cash left no longer has cash received / change to give back
    if not any(p.mode == "cash" for p in payments):
        sale.cash_received = None
        sale.change_given = None
    await write_audit(
        db, user_id=current.user_id, role_code=current.role_code, store_id=sale.store_id, device_id=current.device_id,
        action="sale.payment_mode_changed", entity_type="sale", entity_id=sale.id,
        old_value={"mode": payload.from_mode, "amount": payload.amount}, new_value={"mode": payload.to_mode, "amount": payload.amount},
        reason=payload.reason,
    )
    await db.commit()
    return {"sale_id": str(sale.id), "mode": payload.to_mode}


class SaleVoid(BaseModel):
    reason: str = Field(min_length=5, max_length=300)


async def _put_back_price_stock(db: AsyncSession, sale: Sale) -> None:
    """Units sold at an older price go back into that price's share of the store's stock."""
    from app.models.models import PriceVariantStock, ProductPriceVariant
    from app.services.price_stock import match_variant

    ids = list({i.product_id for i in sale.items})
    rows = (await db.execute(select(ProductPriceVariant).where(ProductPriceVariant.product_id.in_(ids), ProductPriceVariant.is_active.is_(True)))).scalars().all()
    if not rows:
        return
    by_product: dict[uuid.UUID, list[dict]] = {}
    for v in rows:
        by_product.setdefault(v.product_id, []).append({"id": v.id, "mrp": float(v.mrp), "selling_price": float(v.selling_price)})
    back: dict[str, float] = {}
    for item in sale.items:
        vid = match_variant(by_product.get(item.product_id, []), float(item.mrp_snapshot) if item.mrp_snapshot is not None else None, float(item.unit_price))
        if vid:
            back[vid] = back.get(vid, 0) + float(item.quantity)
    for vid, qty in back.items():
        share = await db.get(PriceVariantStock, (uuid.UUID(vid), sale.store_id))
        if share is not None:
            share.quantity = round(float(share.quantity) + qty, 3)


@router.post("/{sale_id}/void")
async def void_sale(
    sale_id: uuid.UUID,
    payload: SaleVoid,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("pos.shift.override")),
) -> dict:
    """Cancel a whole bill that was rung up wrongly, so it can be billed again correctly. A manager's action with a reason, kept in
    the audit log. The stock goes back, the loyalty points and wallet money are put back, and credit given on it is taken off the
    customer's account. Refused when the till's shift or the store's day is closed, when a return was already made against it, or
    when the money came through the Paytm machine (refund that on the machine) or a gift voucher."""
    from app.models.models_phase2 import Return

    sale = await db.get(Sale, sale_id)
    if sale is None or sale.status != "completed":
        raise HTTPException(status_code=404, detail="Bill not found, or it is already cancelled")
    require_store_access(sale.store_id, current)

    billed = sale.billed_at if sale.billed_at.tzinfo else sale.billed_at.replace(tzinfo=timezone.utc)
    closed_shift = await db.scalar(
        select(CashierShift.id).where(CashierShift.device_id == sale.device_id, CashierShift.status == "closed", CashierShift.opened_at <= billed, CashierShift.closed_at >= billed).limit(1)
    )
    if closed_shift is not None:
        raise HTTPException(status_code=409, detail="That till's shift is already closed and counted. Reopen the shift first (emergency) if this bill must be cancelled")
    if await db.scalar(select(DayClose.id).where(DayClose.store_id == sale.store_id, DayClose.business_date == billed.date()).limit(1)) is not None:
        raise HTTPException(status_code=409, detail="The store's day is closed, so this bill can no longer be cancelled")
    if await db.scalar(select(Return.id).where(Return.sale_id == sale_id, Return.status != "rejected").limit(1)) is not None:
        raise HTTPException(status_code=409, detail="A return was already made on this bill. Cancel is not possible; use returns for the rest")

    payments = (await db.execute(select(Payment).where(Payment.sale_id == sale_id))).scalars().all()
    for p in payments:
        if p.mode == "gift_voucher":
            raise HTTPException(status_code=409, detail="This bill used a gift voucher, which cannot be restored automatically. Use a return instead")
        confirmed = await db.scalar(
            select(PaytmPosPayment.id).where(PaytmPosPayment.merchant_transaction_id == (p.reference or ""), PaytmPosPayment.status == "success").limit(1)
        )
        if confirmed is not None:
            raise HTTPException(status_code=409, detail="The Paytm machine confirmed a payment on this bill. Refund it on the machine, then use a return")

    # stock back, once per product (the same product can be on a bill twice)
    by_product: dict[uuid.UUID, float] = {}
    for item in (await db.execute(select(SaleItem).where(SaleItem.sale_id == sale_id))).scalars().all():
        by_product[item.product_id] = by_product.get(item.product_id, 0) + float(item.quantity)
    for product_id, qty in by_product.items():
        await apply_movement(
            db, product_id=product_id, store_id=sale.store_id, delta=qty, reason_code="sale_void", source_type="sale_void",
            source_id=sale.id, created_by=current.user_id, device_id=None,
        )
    await _put_back_price_stock(db, sale)

    if sale.customer_id is not None:
        if float(sale.loyalty_points_earned) > 0:
            try:
                await apply_ledger_entry(db, customer_id=sale.customer_id, delta_points=-float(sale.loyalty_points_earned), reason="void_reversal_earn", source_type="sale_void", source_id=sale.id)
            except DuplicateLedgerEntry:
                pass
        if float(sale.loyalty_points_redeemed) > 0:
            try:
                await apply_ledger_entry(db, customer_id=sale.customer_id, delta_points=float(sale.loyalty_points_redeemed), reason="void_reversal_redeem", source_type="sale_void", source_id=sale.id)
            except DuplicateLedgerEntry:
                pass
        for p in payments:
            if p.mode == "wallet" and float(p.amount) > 0:
                try:
                    await credit_wallet(db, customer_id=sale.customer_id, amount=float(p.amount), reference_type="sale_void", source_type="sale_void", source_id=sale.id)
                except DuplicateWalletEntry:
                    pass
            elif p.mode == "credit" and float(p.amount) > 0:
                db.add(CreditEntry(customer_id=sale.customer_id, store_id=sale.store_id, kind="adjustment", amount=-float(p.amount), sale_id=sale.id, note=f"Bill {sale.bill_number} cancelled", created_by=current.user_id))

    sale.status = "voided"
    await write_audit(
        db, user_id=current.user_id, role_code=current.role_code, store_id=sale.store_id, device_id=current.device_id,
        action="sale.voided", entity_type="sale", entity_id=sale.id,
        old_value={"bill_number": sale.bill_number, "grand_total": float(sale.grand_total)}, new_value={"status": "voided"}, reason=payload.reason,
    )
    await db.commit()
    return {"sale_id": str(sale.id), "status": "voided"}
