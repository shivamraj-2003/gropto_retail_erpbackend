"""Manual accounting vouchers, saved on the server and posted into the ledger."""

import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission, require_store_access
from app.core.database import get_db
from app.models.models import Purchase, Store, Vendor
from app.models.models_finance import FinanceVoucher, FinanceVoucherAllocation, FinanceVoucherLine
from app.models.models_phase2 import Payable
from app.services.audit import write_audit

router = APIRouter(prefix="/finance-advanced/vouchers", tags=["finance-vouchers"])

TYPES = ("CASH RECEIPT", "CASH PAYMENT", "BANK RECEIPT", "BANK PAYMENT", "CONTRA", "JOURNAL", "OPENING")
PREFIX = {"CASH RECEIPT": "CR", "CASH PAYMENT": "CP", "BANK RECEIPT": "BR", "BANK PAYMENT": "BP", "CONTRA": "CV", "JOURNAL": "JV", "OPENING": "OB"}
HEADS = ("capital", "current_assets", "current_liabilities", "fixed", "investment", "loan", "misc", "suspense", "income", "direct", "indirect")
# the letters in a voucher's number, e.g. ST00126BPV-0001
SERIES = {"CASH RECEIPT": "CRV", "CASH PAYMENT": "CPV", "BANK RECEIPT": "BRV", "BANK PAYMENT": "BPV", "CONTRA": "CNV", "JOURNAL": "JNV", "OPENING": "OBV"}
KINDS = ("on_account", "against", "new_ref", "advance")
FIXED_KEYS = {"cash", "bank", "upi", "card", "other_tender", "udhaar", "sales", "gst_out", "returns", "purchases", "gst_in"}


class AllocationIn(BaseModel):
    kind: str = "on_account"
    payable_id: uuid.UUID | None = None
    bill_no: str | None = Field(default=None, max_length=60)
    amount: float = Field(ge=0)
    deduction: float = Field(default=0, ge=0)
    remarks: str | None = Field(default=None, max_length=200)


class LineIn(BaseModel):
    dc: str = "D"  # D or C (receipts and payments decide it themselves)
    account: str = Field(min_length=1)  # a ledger account key, or a new account's name
    account_name: str | None = None
    account_group: str | None = None  # the head of a shop-named account, see HEADS
    amount: float = Field(gt=0)
    mode_type: str | None = None
    mode_no: str | None = None
    mode_date: date | None = None
    cost_center: str | None = Field(default=None, max_length=60)
    department: str | None = Field(default=None, max_length=60)
    employee: str | None = Field(default=None, max_length=60)
    allocations: list[AllocationIn] = []


class VoucherIn(BaseModel):
    store_id: uuid.UUID
    voucher_type: str
    voucher_date: date
    voucher_no: str | None = None
    main_account: str | None = None
    main_account_name: str | None = None
    narration: str | None = Field(default=None, max_length=500)
    lines: list[LineIn] = Field(min_length=1)
    reco_date: date | None = None
    replace_id: uuid.UUID | None = None  # editing a voucher: the old one is taken back and this one saved in its place, together


async def next_number(db: AsyncSession, store_id: uuid.UUID, vtype: str, day: date) -> str:
    code = await db.scalar(select(Store.code).where(Store.id == store_id)) or "ST"
    year = (day.year if day.month >= 4 else day.year - 1) % 100  # the financial year runs April to March
    series = f"{code}{year:02d}{SERIES[vtype]}"
    used = (await db.execute(select(FinanceVoucher.voucher_no).where(FinanceVoucher.store_id == store_id, FinanceVoucher.voucher_no.like(f"{series}-%")))).scalars().all()
    highest = max((int(n.rsplit("-", 1)[1]) for n in used if n.rsplit("-", 1)[1].isdigit()), default=0)
    return f"{series}-{highest + 1:04d}"


def _payable_status(p: Payable) -> str:
    if float(p.amount_due) <= 0.005:
        return "paid"
    return "partially_paid" if float(p.amount_due) < float(p.original_amount or p.amount_due) - 0.005 else "outstanding"


async def take_back(db: AsyncSession, voucher: FinanceVoucher) -> None:
    """Give the bills a voucher had settled their due amounts back, then remove the voucher."""
    for line in voucher.lines:
        for a in line.allocations:
            if a.payable_id is not None:
                payable = await db.get(Payable, a.payable_id, with_for_update=True)
                if payable is not None:
                    payable.amount_due = float(payable.amount_due) + float(a.amount) + float(a.deduction)
                    payable.status = _payable_status(payable)
    await db.delete(voucher)
    await db.flush()


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

    replaced_number = None
    if payload.replace_id is not None:
        old = await db.get(FinanceVoucher, payload.replace_id)
        if old is None or old.store_id != payload.store_id:
            raise HTTPException(status_code=404, detail="The voucher being edited no longer exists")
        replaced_number = old.voucher_no if old.voucher_type == vtype else None
        await take_back(db, old)

    # bill-by-bill: what is set against the bills must be the line's amount, and no bill can be paid beyond what is due on it
    for dc, key, name, ln in lines:
        if not ln.allocations:
            continue
        if not key.startswith("vendor:") or dc != "D":
            raise HTTPException(status_code=422, detail=f"Bills can be adjusted only on a payment to a vendor ({name})")
        if abs(round(sum(a.amount for a in ln.allocations), 2) - round(ln.amount, 2)) > 0.005:
            raise HTTPException(status_code=422, detail=f"{name}: the amounts against the bills must add up to {ln.amount:.2f}")
        for a in ln.allocations:
            if a.kind not in KINDS:
                raise HTTPException(status_code=422, detail="Unknown adjustment type")
            if a.kind != "against":
                a.payable_id, a.deduction = None, 0
                continue
            payable = await db.get(Payable, a.payable_id, with_for_update=True) if a.payable_id else None
            purchase = await db.get(Purchase, payable.purchase_id) if payable is not None and payable.purchase_id else None
            if payable is None or f"vendor:{payable.vendor_id}" != key or purchase is None or purchase.store_id != payload.store_id:
                raise HTTPException(status_code=422, detail=f"{name}: choose one of this vendor's open bills")
            settled = round(a.amount + a.deduction, 2)
            if settled > float(payable.amount_due) + 0.005:
                raise HTTPException(status_code=422, detail=f"{name}: bill {a.bill_no or ''} has only {float(payable.amount_due):.2f} due")
            payable.amount_due = max(float(payable.amount_due) - settled, 0)
            payable.status = _payable_status(payable)

    number = (payload.voucher_no or "").strip() or replaced_number or ""
    if not number:
        number = await next_number(db, payload.store_id, vtype, payload.voucher_date)
    if await db.scalar(select(FinanceVoucher.id).where(FinanceVoucher.store_id == payload.store_id, FinanceVoucher.voucher_no == number)):
        raise HTTPException(status_code=409, detail=f"Voucher number {number} is already used")

    voucher = FinanceVoucher(
        store_id=payload.store_id, voucher_type=vtype, voucher_no=number, voucher_date=payload.voucher_date, main_account=main_key,
        main_account_name=main_name, narration=payload.narration, reco_date=payload.reco_date, total_amount=total, created_by=current.user_id,
    )
    for dc, key, name, ln in lines:
        voucher.lines.append(
            FinanceVoucherLine(dc=dc, account_key=key, account_group=(ln.account_group if ln.account_group in HEADS and key.startswith("custom:") else None), account_name=name, amount=ln.amount, mode_type=ln.mode_type, mode_no=ln.mode_no, mode_date=ln.mode_date,
                               cost_center=ln.cost_center, department=ln.department, employee=ln.employee,
                               allocations=[FinanceVoucherAllocation(kind=a.kind, payable_id=a.payable_id, bill_no=a.bill_no, amount=a.amount, deduction=a.deduction, remarks=a.remarks) for a in ln.allocations])
        )
    db.add(voucher)
    await db.flush()
    await write_audit(
        db, user_id=current.user_id, role_code=current.role_code, store_id=payload.store_id, device_id=current.device_id,
        action="finance.voucher_created", entity_type="finance_voucher", entity_id=voucher.id, new_value={"voucher_no": number, "type": vtype, "total": total},
    )
    await db.commit()
    return {"id": str(voucher.id), "voucher_no": number, "total_amount": total}


@router.get("/next-number")
async def preview_number(
    store_id: uuid.UUID,
    voucher_type: str,
    voucher_date: date,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("finance.pnl.view")),
) -> dict:
    require_store_access(store_id, current)
    if voucher_type.upper() not in TYPES:
        raise HTTPException(status_code=422, detail="Unknown voucher type")
    return {"voucher_no": await next_number(db, store_id, voucher_type.upper(), voucher_date)}


@router.get("/party")
async def party_bills(
    store_id: uuid.UUID,
    vendor_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("finance.pnl.view")),
) -> dict:
    """A vendor's bills that still have money due, for setting a payment against them."""
    require_store_access(store_id, current)
    vendor = await db.get(Vendor, vendor_id)
    if vendor is None:
        raise HTTPException(status_code=404, detail="Vendor not found")
    rows = (
        await db.execute(
            select(Payable, Purchase)
            .join(Purchase, Purchase.id == Payable.purchase_id)
            .where(Payable.vendor_id == vendor_id, Purchase.store_id == store_id, Payable.amount_due > 0.005)
            .order_by(Purchase.invoice_date.asc().nullsfirst(), Purchase.created_at)
        )
    ).all()
    return {
        "name": vendor.name,
        "bills": [
            {
                "payable_id": str(p.id), "bill_no": pu.invoice_number or f"no number ({pu.created_at:%d/%m})",
                "bill_date": pu.invoice_date.isoformat() if pu.invoice_date else None, "due_date": p.due_date.isoformat() if p.due_date else None,
                "amount": float(p.original_amount or 0), "due": float(p.amount_due),
            }
            for p, pu in rows
        ],
    }


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
            "main_account": v.main_account_name, "main_account_key": v.main_account, "narration": v.narration, "total_amount": float(v.total_amount),
            "reco_date": v.reco_date.isoformat() if v.reco_date else None,
            "lines": [
                {
                    "dc": l.dc, "account": l.account_name, "account_key": l.account_key, "account_group": l.account_group, "amount": float(l.amount),
                    "mode_type": l.mode_type, "mode_no": l.mode_no, "mode_date": l.mode_date.isoformat() if l.mode_date else None,
                    "cost_center": l.cost_center, "department": l.department, "employee": l.employee,
                    "allocations": [
                        {"kind": a.kind, "payable_id": str(a.payable_id) if a.payable_id else None, "bill_no": a.bill_no, "amount": float(a.amount), "deduction": float(a.deduction), "remarks": a.remarks}
                        for a in l.allocations
                    ],
                }
                for l in v.lines
            ],
        }
        for v in rows
    ]


@router.delete("/{voucher_id}", status_code=204)
async def delete_voucher(
    voucher_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("finance.expense.create")),
) -> None:
    voucher = await db.get(FinanceVoucher, voucher_id)
    if voucher is None:
        raise HTTPException(status_code=404, detail="Voucher not found")
    require_store_access(voucher.store_id, current)
    old = {"voucher_no": voucher.voucher_no, "type": voucher.voucher_type, "total": float(voucher.total_amount), "date": voucher.voucher_date.isoformat(),
           "lines": [{"dc": l.dc, "account": l.account_name, "amount": float(l.amount)} for l in voucher.lines]}
    store_id = voucher.store_id
    await take_back(db, voucher)
    await write_audit(
        db, user_id=current.user_id, role_code=current.role_code, store_id=store_id, device_id=current.device_id,
        action="finance.voucher_deleted", entity_type="finance_voucher", entity_id=voucher_id, old_value=old,
    )
    await db.commit()
