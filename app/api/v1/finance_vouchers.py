"""Manual accounting vouchers, saved on the server and posted into the ledger."""

import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission, require_store_access
from app.core.database import get_db
from app.models.models_finance import FinanceVoucher, FinanceVoucherLine
from app.services.audit import write_audit

router = APIRouter(prefix="/finance-advanced/vouchers", tags=["finance-vouchers"])

TYPES = ("CASH RECEIPT", "CASH PAYMENT", "BANK RECEIPT", "BANK PAYMENT", "CONTRA", "JOURNAL", "OPENING")
PREFIX = {"CASH RECEIPT": "CR", "CASH PAYMENT": "CP", "BANK RECEIPT": "BR", "BANK PAYMENT": "BP", "CONTRA": "CV", "JOURNAL": "JV", "OPENING": "OB"}
FIXED_KEYS = {"cash", "bank", "upi", "card", "other_tender", "udhaar", "sales", "gst_out", "returns", "purchases", "gst_in"}


class LineIn(BaseModel):
    dc: str = "D"  # D or C (receipts and payments decide it themselves)
    account: str = Field(min_length=1)  # a ledger account key, or a new account's name
    account_name: str | None = None
    amount: float = Field(gt=0)
    mode_type: str | None = None
    mode_no: str | None = None
    mode_date: date | None = None


class VoucherIn(BaseModel):
    store_id: uuid.UUID
    voucher_type: str
    voucher_date: date
    voucher_no: str | None = None
    main_account: str | None = None
    main_account_name: str | None = None
    narration: str | None = Field(default=None, max_length=500)
    lines: list[LineIn] = Field(min_length=1)


def account_key(raw: str) -> tuple[str, str | None]:
    """A known account key is kept; anything else is a new account named by the user."""
    raw = raw.strip()
    if raw in FIXED_KEYS or raw.startswith(("vendor:", "expense:", "custom:")):
        return raw, None
    return f"custom:{raw.upper()}", raw.upper()


@router.post("", status_code=201)
async def create_voucher(
    payload: VoucherIn,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("finance.expense.create")),
) -> dict:
    require_store_access(payload.store_id, current)
    vtype = payload.voucher_type.upper()
    if vtype not in TYPES:
        raise HTTPException(status_code=422, detail="Unknown voucher type")
    receipt = vtype.endswith("RECEIPT")
    two_sided = vtype in ("JOURNAL", "OPENING")

    lines: list[tuple[str, str, str, LineIn]] = []  # dc, key, name, line
    for ln in payload.lines:
        key, new_name = account_key(ln.account)
        name = ln.account_name or new_name or key
        dc = ("C" if receipt else "D") if not two_sided else ln.dc.upper()
        if dc not in ("D", "C"):
            raise HTTPException(status_code=422, detail="Each line must be debit (D) or credit (C)")
        lines.append((dc, key, name, ln))

    total = round(sum(float(l[3].amount) for l in lines), 2)
    main_key = main_name = None
    if two_sided:
        debit = round(sum(float(l[3].amount) for l in lines if l[0] == "D"), 2)
        credit = round(sum(float(l[3].amount) for l in lines if l[0] == "C"), 2)
        if abs(debit - credit) > 0.005:
            raise HTTPException(status_code=422, detail=f"Debit ₹{debit:.2f} and credit ₹{credit:.2f} must be equal")
        total = debit
    else:
        if not payload.main_account:
            raise HTTPException(status_code=422, detail="Choose the cash or bank account the money moved through")
        main_key, new = account_key(payload.main_account)
        main_name = payload.main_account_name or new or main_key
        if any(l[1] == main_key for l in lines):
            raise HTTPException(status_code=422, detail="The account on a line cannot be the same as the cash or bank account")

    number = (payload.voucher_no or "").strip()
    if not number:
        count = await db.scalar(
            select(func.count()).select_from(FinanceVoucher).where(FinanceVoucher.store_id == payload.store_id, FinanceVoucher.voucher_type == vtype)
        )
        number = f"{PREFIX[vtype]}-{payload.voucher_date:%Y%m%d}-{int(count or 0) + 1:04d}"
    if await db.scalar(select(FinanceVoucher.id).where(FinanceVoucher.store_id == payload.store_id, FinanceVoucher.voucher_no == number)):
        raise HTTPException(status_code=409, detail=f"Voucher number {number} is already used")

    voucher = FinanceVoucher(
        store_id=payload.store_id, voucher_type=vtype, voucher_no=number, voucher_date=payload.voucher_date, main_account=main_key,
        main_account_name=main_name, narration=payload.narration, total_amount=total, created_by=current.user_id,
    )
    for dc, key, name, ln in lines:
        voucher.lines.append(
            FinanceVoucherLine(dc=dc, account_key=key, account_name=name, amount=ln.amount, mode_type=ln.mode_type, mode_no=ln.mode_no, mode_date=ln.mode_date)
        )
    db.add(voucher)
    await db.flush()
    await write_audit(
        db, user_id=current.user_id, role_code=current.role_code, store_id=payload.store_id, device_id=current.device_id,
        action="finance.voucher_created", entity_type="finance_voucher", entity_id=voucher.id, new_value={"voucher_no": number, "type": vtype, "total": total},
    )
    await db.commit()
    return {"id": str(voucher.id), "voucher_no": number, "total_amount": total}


@router.get("")
async def list_vouchers(
    store_id: uuid.UUID,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int = 200,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("finance.pnl.view")),
) -> list[dict]:
    require_store_access(store_id, current)
    stmt = select(FinanceVoucher).where(FinanceVoucher.store_id == store_id)
    if date_from:
        stmt = stmt.where(FinanceVoucher.voucher_date >= date_from)
    if date_to:
        stmt = stmt.where(FinanceVoucher.voucher_date <= date_to)
    rows = (await db.execute(stmt.order_by(FinanceVoucher.voucher_date.desc(), FinanceVoucher.created_at.desc()).limit(max(1, min(limit, 500))))).scalars().all()
    return [
        {
            "id": str(v.id), "voucher_no": v.voucher_no, "voucher_type": v.voucher_type, "voucher_date": v.voucher_date.isoformat(),
            "main_account": v.main_account_name, "narration": v.narration, "total_amount": float(v.total_amount),
            "lines": [{"dc": l.dc, "account": l.account_name, "amount": float(l.amount), "mode_type": l.mode_type, "mode_no": l.mode_no} for l in v.lines],
        }
        for v in rows
    ]
