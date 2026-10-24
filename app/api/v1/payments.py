import uuid
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission, require_store_access
from app.core.database import get_db
from app.models.models_paytm import PaytmPosPayment, PaytmTerminal
from app.schemas.schemas import PaymentConfigOut
from app.services import paytm_flow as flow
from app.services import paytm_pos as paytm
from app.services.audit import write_audit

router = APIRouter(prefix="/payments", tags=["payments"])


@router.get("/config", response_model=PaymentConfigOut)
async def payment_config(
    store_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("pos.payment.view")),
):
    """Is the Paytm machine available for this store? When not, the till keeps its plain tender buttons."""
    if not paytm.is_configured():
        return PaymentConfigOut(enabled=False)
    if store_id is not None:
        require_store_access(store_id, current)
        found = await db.scalar(
            select(PaytmTerminal.id).where(PaytmTerminal.store_id == store_id, PaytmTerminal.is_active.is_(True)).limit(1)
        )
        if found is None:
            return PaymentConfigOut(enabled=False)
    return PaymentConfigOut(enabled=True, environment=paytm.settings.paytm_environment)


# --------------------------------------------------------------------------- terminals

class TerminalIn(BaseModel):
    store_id: uuid.UUID
    mid: str = Field(min_length=1, max_length=64)
    tid: str = Field(min_length=1, max_length=16)
    device_serial: str | None = Field(default=None, max_length=64)
    label: str | None = Field(default=None, max_length=80)
    is_active: bool = True


class TerminalOut(BaseModel):
    id: uuid.UUID
    store_id: uuid.UUID
    mid: str
    tid: str
    device_serial: str | None
    label: str | None
    is_active: bool


def _terminal_out(t: PaytmTerminal) -> TerminalOut:
    return TerminalOut(id=t.id, store_id=t.store_id, mid=t.mid, tid=t.tid, device_serial=t.device_serial, label=t.label, is_active=t.is_active)


@router.get("/paytm-pos/terminals", response_model=list[TerminalOut])
async def list_terminals(
    store_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("pos.payment.view")),
):
    require_store_access(store_id, current)
    rows = (await db.scalars(select(PaytmTerminal).where(PaytmTerminal.store_id == store_id).order_by(PaytmTerminal.created_at))).all()
    return [_terminal_out(t) for t in rows]


@router.put("/paytm-pos/terminals", response_model=TerminalOut)
async def save_terminal(
    body: TerminalIn,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("device.terminal.create")),
):
    """Map a Paytm machine (MID + TID) to a store, or change/switch off an existing mapping."""
    require_store_access(body.store_id, current)
    existing = await db.scalar(select(PaytmTerminal).where(PaytmTerminal.mid == body.mid, PaytmTerminal.tid == body.tid))
    if existing is not None and existing.store_id != body.store_id:
        raise HTTPException(status_code=409, detail="That machine is already set up for another store")
    old = None
    if existing is None:
        existing = PaytmTerminal(store_id=body.store_id, mid=body.mid, tid=body.tid)
        db.add(existing)
    else:
        old = {"label": existing.label, "device_serial": existing.device_serial, "is_active": existing.is_active}
    existing.device_serial = body.device_serial
    existing.label = body.label
    existing.is_active = body.is_active
    await db.flush()
    await write_audit(
        db, user_id=current.user_id, role_code=current.role_code, store_id=body.store_id, device_id=current.device_id,
        action="paytm.terminal_saved", entity_type="paytm_terminal", entity_id=existing.id,
        old_value=old, new_value={"mid": body.mid, "tid": body.tid, "label": body.label, "is_active": body.is_active},
    )
    await db.commit()
    return _terminal_out(existing)


# --------------------------------------------------------------------------- payments

class InitiateIn(BaseModel):
    store_id: uuid.UUID
    amount: Decimal = Field(gt=0)
    # made once per button press by the till; pressing twice (or a retry after a timeout) returns the same request
    idempotency_key: str = Field(min_length=8, max_length=64)
    terminal_id: uuid.UUID | None = None
    receipt_label: str | None = Field(default=None, max_length=60)


class PaytmPaymentOut(BaseModel):
    id: uuid.UUID
    store_id: uuid.UUID
    terminal_id: uuid.UUID
    terminal_tid: str | None = None
    merchant_transaction_id: str
    amount: float
    status: str  # created | sent | success | failed | cancel_requested | cancelled | unknown
    message: str | None
    paid: bool  # true only when Paytm confirmed the payment
    slow: bool = False
    reference: str | None = None  # what the bill stores as the payment reference once paid
    details: dict | None = None
    created_at: datetime | None = None


async def _out(db: AsyncSession, p: PaytmPosPayment) -> PaytmPaymentOut:
    terminal = await db.get(PaytmTerminal, p.terminal_id)
    return PaytmPaymentOut(
        id=p.id, store_id=p.store_id, terminal_id=p.terminal_id, terminal_tid=terminal.tid if terminal else None,
        merchant_transaction_id=p.merchant_transaction_id, amount=float(p.amount), status=p.status, message=p.status_message,
        paid=p.status == paytm.SUCCESS, slow=flow.is_slow(p, flow.now_utc()),
        reference=p.merchant_transaction_id if p.status == paytm.SUCCESS else None,
        details=p.paytm_details, created_at=p.created_at,
    )


async def _move(db: AsyncSession, p: PaytmPosPayment, current: CurrentUser, new_status: str, message: str, *, code: str | None = None) -> None:
    """Change a payment's state and write it to the audit log in the same transaction."""
    old = p.status
    p.status = new_status
    p.status_message = message[:250]
    if code:
        p.result_code = code
    if new_status in flow.FINAL_STATES and p.completed_at is None:
        p.completed_at = flow.now_utc()
    if old != new_status:
        await write_audit(
            db, user_id=current.user_id, role_code=current.role_code, store_id=p.store_id, device_id=current.device_id,
            action="paytm.payment_status", entity_type="paytm_payment", entity_id=p.id,
            old_value={"status": old}, new_value={"status": new_status, "message": message[:250], "amount": float(p.amount)},
        )


def _as_http(exc: flow.FlowError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


@router.post("/paytm-pos/initiate", response_model=PaytmPaymentOut)
async def initiate(
    body: InitiateIn,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("pos.payment.create")),
):
    """Send the bill's amount to the store's Paytm machine. The bill is NOT paid by this call; only a later
    status of 'success' (read from Paytm) means money arrived."""
    require_store_access(body.store_id, current)
    again = await db.scalar(select(PaytmPosPayment).where(PaytmPosPayment.idempotency_key == body.idempotency_key))
    if again is not None:
        if again.store_id != body.store_id or paytm.to_paise(again.amount) != paytm.to_paise(body.amount):
            raise HTTPException(status_code=409, detail="That request key was already used for a different payment")
        return await _out(db, again)
    if not paytm.is_configured():
        raise HTTPException(status_code=409, detail="Paytm POS is not set up on the server (merchant key or channel id missing)")
    try:
        amount = flow.check_amount(body.amount)
        terminals = (await db.scalars(select(PaytmTerminal).where(PaytmTerminal.store_id == body.store_id))).all()
        terminal = flow.choose_terminal(list(terminals), body.terminal_id)
    except flow.FlowError as exc:
        raise _as_http(exc) from exc

    # One request at a time per machine: lock the machine row so two tills pressing together cannot both pass the check
    await db.scalar(select(PaytmTerminal.id).where(PaytmTerminal.id == terminal.id).with_for_update())
    open_rows = (
        await db.scalars(
            select(PaytmPosPayment).where(PaytmPosPayment.terminal_id == terminal.id, PaytmPosPayment.status.in_(paytm.OPEN_STATES))
        )
    ).all()
    blocker = flow.blocking_payment(list(open_rows))
    if blocker is not None:
        raise HTTPException(status_code=409, detail=flow.blocking_message(blocker))

    p = PaytmPosPayment(
        store_id=body.store_id, terminal_id=terminal.id, merchant_transaction_id=paytm.new_merchant_transaction_id(),
        idempotency_key=body.idempotency_key, amount=amount, status=paytm.CREATED, receipt_label=body.receipt_label,
        created_by=current.user_id,
    )
    db.add(p)
    await db.flush()
    await write_audit(
        db, user_id=current.user_id, role_code=current.role_code, store_id=p.store_id, device_id=current.device_id,
        action="paytm.payment_initiated", entity_type="paytm_payment", entity_id=p.id,
        new_value={"amount": float(amount), "tid": terminal.tid, "merchant_transaction_id": p.merchant_transaction_id},
    )
    await db.commit()  # saved before Paytm is asked, so a crash after sending can still be reconciled

    request = paytm.sale_body(
        mid=terminal.mid, tid=terminal.tid, merchant_txn_id=p.merchant_transaction_id, amount_rupees=amount,
        reference=body.receipt_label, when=paytm.now_ist(),
    )
    try:
        answer = await paytm.send_sale(request)
    except paytm.PaytmError as exc:
        # We do not know whether the request got through, so it is UNKNOWN (checked with Paytm before anything new is sent)
        await _move(db, p, current, paytm.UNKNOWN, f"{exc}. Checking with Paytm")
        await db.commit()
        return await _out(db, p)
    state, message = paytm.read_sale_answer(answer)
    if state == paytm.SENT:
        p.sent_at = flow.now_utc()
    await _move(db, p, current, state, message, code=paytm.result_of(answer)["code"])
    await db.commit()
    return await _out(db, p)


async def _load(db: AsyncSession, payment_id: uuid.UUID, current: CurrentUser) -> PaytmPosPayment:
    p = await db.scalar(select(PaytmPosPayment).where(PaytmPosPayment.id == payment_id).with_for_update())
    if p is None:
        raise HTTPException(status_code=404, detail="Payment not found")
    require_store_access(p.store_id, current)
    return p


def _ist(moment: datetime | None) -> str:
    return (moment or flow.now_utc()).astimezone(paytm.IST).strftime("%Y-%m-%d %H:%M:%S")


@router.get("/paytm-pos/status/{payment_id}", response_model=PaytmPaymentOut)
async def status(
    payment_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("pos.payment.view")),
):
    """Asks Paytm (never the browser) where the payment stands. Safe to call every couple of seconds: Paytm itself is
    asked at most every 10 seconds."""
    p = await _load(db, payment_id, current)
    now = flow.now_utc()
    if not flow.due_for_enquiry(p, now):
        await db.rollback()
        return await _out(db, p)
    terminal = await db.get(PaytmTerminal, p.terminal_id)
    void = p.status == paytm.CANCEL_REQUESTED
    request = paytm.lookup_body(
        mid=terminal.mid, tid=terminal.tid, merchant_txn_id=p.merchant_transaction_id, when=_ist(p.created_at), void=void
    )
    p.last_enquiry_at = now
    p.enquiry_count = (p.enquiry_count or 0) + 1
    try:
        answer = await paytm.enquire(request)
    except paytm.PaytmError as exc:
        p.status_message = f"{exc}. Still checking"[:250]
        await db.commit()
        return await _out(db, p)
    state, message = paytm.read_status_answer(answer, void=void)
    result = paytm.result_of(answer)
    amount_ok = paytm.amount_matches(answer, p.amount) if state == paytm.SUCCESS and not void else True
    new_state, new_message = flow.next_state(p, state, message, code=result["code"], amount_ok=amount_ok, now=now)
    if new_state == paytm.SUCCESS:
        p.paytm_details = paytm.paid_details(answer)
    await _move(db, p, current, new_state, new_message, code=result["code"])
    await db.commit()
    return await _out(db, p)


class CancelIn(BaseModel):
    payment_id: uuid.UUID


@router.post("/paytm-pos/cancel", response_model=PaytmPaymentOut)
async def cancel(
    body: CancelIn,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("pos.payment.create")),
):
    """Asks Paytm to void the request. The state only becomes 'cancelled' when Paytm confirms it; a payment that
    turns out to be paid stays paid."""
    p = await _load(db, body.payment_id, current)
    if p.status == paytm.SUCCESS:
        raise HTTPException(status_code=409, detail="This payment was already paid. It cannot be cancelled here; refund it on the machine")
    if p.status in (paytm.FAILED, paytm.CANCELLED):
        return await _out(db, p)
    terminal = await db.get(PaytmTerminal, p.terminal_id)
    request = paytm.lookup_body(mid=terminal.mid, tid=terminal.tid, merchant_txn_id=p.merchant_transaction_id, when=_ist(p.created_at))
    try:
        answer = await paytm.send_void(request)
    except paytm.PaytmError as exc:
        raise HTTPException(status_code=502, detail=f"{exc}. The payment was not cancelled") from exc
    state, message = paytm.read_sale_answer(answer)
    if state != paytm.SENT:
        raise HTTPException(status_code=409, detail=f"Paytm did not accept the cancel: {message}")
    p.last_enquiry_at = None  # check the cancel straight away
    await _move(db, p, current, paytm.CANCEL_REQUESTED, "Cancel sent to Paytm")
    await db.commit()
    return await _out(db, p)
