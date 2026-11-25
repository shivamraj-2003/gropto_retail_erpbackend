"""The ledger, built from the shop's real records (no separate vouchers to key in): sales and the way they were paid, GST collected,
cash in and out, bank deposits, expenses, purchases and the GST on them, what each vendor is owed and has been paid, customers'
credit (udhaar) and returns. Every figure is added up from the tables that already hold it, by store, and for the warehouse level
from what was received into warehouses."""

import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission, require_store_access
from app.core.database import get_db

router = APIRouter(prefix="/finance-advanced/ledger", tags=["ledger"])

IST_DAY = "(timezone('Asia/Kolkata', {col}))::date"
MONTHS = ["", "JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY", "AUGUST", "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER"]

# key -> (name, group). Vendor and expense accounts are added as they are found.
FIXED = {
    "cash": ("Cash in hand", "Cash & bank"),
    "bank": ("Bank (cash deposited)", "Cash & bank"),
    "upi": ("UPI receipts", "Cash & bank"),
    "card": ("Card receipts", "Cash & bank"),
    "other_tender": ("Wallet, vouchers & loyalty", "Cash & bank"),
    "udhaar": ("Customers on credit (Udhaar)", "Customers"),
    "sales": ("Sales (before GST)", "Sales & tax"),
    "gst_out": ("GST collected on sales", "Sales & tax"),
    "returns": ("Sales returns (refunds)", "Sales & tax"),
    "purchases": ("Purchases (before GST)", "Purchases"),
    "gst_in": ("GST paid on purchases", "Purchases"),
}


class Book:
    """account key -> {day: [debit, credit]}, with the names and groups of the accounts that have something in them."""

    def __init__(self) -> None:
        self.days: dict[str, dict[date, list[float]]] = {}
        self.names: dict[str, tuple[str, str]] = dict(FIXED)

    def post(self, key: str, day: date, debit: float = 0.0, credit: float = 0.0, name: str | None = None, group: str | None = None) -> None:
        if not (debit or credit):
            return
        if name and key not in self.names:
            self.names[key] = (name, group or "Other")
        cell = self.days.setdefault(key, {}).setdefault(day, [0.0, 0.0])
        cell[0] += float(debit)
        cell[1] += float(credit)


async def build_book(db: AsyncSession, *, store_id: uuid.UUID, warehouse: bool, warehouse_id: uuid.UUID | None, upto: date) -> Book:
    book = Book()
    p = {"sid": str(store_id), "upto": upto, "wid": str(warehouse_id) if warehouse_id else None}
    rows = lambda q: db.execute(text(q), p)  # noqa: E731

    if not warehouse:
        D = IST_DAY.format(col="s.billed_at")
        for day, taxable, gst in (
            await rows(
                f"""select {D} d, coalesce(sum(si.taxable_value),0), coalesce(sum(si.cgst_amount + si.sgst_amount + si.igst_amount),0)
                    from sales s join sale_items si on si.sale_id = s.id
                    where s.store_id = :sid and s.status = 'completed' and {D} <= :upto group by 1"""
            )
        ).all():
            book.post("sales", day, credit=taxable)
            book.post("gst_out", day, credit=gst)
        for day, mode, amount in (
            await rows(
                f"""select {D}, pay.mode, coalesce(sum(pay.amount),0)
                    from payments pay join sales s on s.id = pay.sale_id
                    where s.store_id = :sid and s.status = 'completed' and {D} <= :upto group by 1, 2"""
            )
        ).all():
            if mode == "cash":
                book.post("cash", day, debit=amount)
            elif mode in ("upi", "card"):
                book.post(mode, day, debit=amount)
            elif mode != "credit":  # credit is the Udhaar account, taken from the credit book below
                book.post("other_tender", day, debit=amount)

        Dm = IST_DAY.format(col="m.created_at")
        for day, direction, amount in (
            await rows(
                f"""select {Dm}, m.direction, coalesce(sum(m.amount),0)
                    from cash_movements m join cashier_shifts sh on sh.id = m.shift_id
                    where sh.store_id = :sid and {Dm} <= :upto group by 1, 2"""
            )
        ).all():
            book.post("cash", day, debit=amount if direction == "in" else 0, credit=amount if direction == "out" else 0)

        for day, amount in (await rows("select business_date, coalesce(sum(cash_deposited),0) from bank_deposits where store_id = :sid and business_date <= :upto group by 1")).all():
            book.post("bank", day, debit=amount)
            book.post("cash", day, credit=amount)  # the cash left the drawer for the bank

        De = IST_DAY.format(col="e.created_at")
        for day, category, amount in (
            await rows(f"select {De}, e.category, coalesce(sum(e.amount),0) from expenses e where e.store_id = :sid and e.status = 'approved' and {De} <= :upto group by 1, 2")
        ).all():
            book.post(f"expense:{category}", day, debit=amount, name=f"Expense: {category}", group="Expenses")

        Dc = IST_DAY.format(col="c.created_at")
        for day, dr, cr in (
            await rows(
                f"""select {Dc}, coalesce(sum(case when c.amount > 0 then c.amount else 0 end),0), coalesce(sum(case when c.amount < 0 then -c.amount else 0 end),0)
                    from credit_entries c where c.store_id = :sid and {Dc} <= :upto group by 1"""
            )
        ).all():
            book.post("udhaar", day, debit=dr, credit=cr)

        Dr = IST_DAY.format(col="r.created_at")
        for day, mode, amount in (
            await rows(
                f"""select {Dr}, coalesce(r.refund_mode, 'cash'), coalesce(sum(r.refund_total),0) from returns r
                    where r.store_id = :sid and r.status = 'completed' and {Dr} <= :upto group by 1, 2"""
            )
        ).all():
            book.post("returns", day, debit=amount)
            book.post(mode if mode in ("cash", "upi", "card") else "cash", day, credit=amount)  # the refund goes out the way it was paid back

        # manual vouchers: what has no bill (money paid from the pocket, a bank charge) and the balances brought forward
        for v_day, v_main, v_main_name, v_type, v_dc, v_key, v_name, v_amt in (
            await rows(
                """select v.voucher_date, v.main_account, v.main_account_name, v.voucher_type, l.dc, l.account_key, l.account_name, l.amount
                   from finance_vouchers v join finance_voucher_lines l on l.voucher_id = v.id
                   where v.store_id = :sid and v.voucher_date <= :upto"""
            )
        ).all():
            def acct(key: str, name: str) -> tuple[str, str | None, str | None]:
                if key in book.names:
                    return key, None, None
                if key.startswith("vendor:") or key.startswith("expense:"):
                    return key, name, "Vendors" if key.startswith("vendor:") else "Expenses"
                return key, name, "Other accounts"
            k, nm, grp = acct(v_key, v_name)
            book.post(k, v_day, debit=float(v_amt) if v_dc == "D" else 0, credit=float(v_amt) if v_dc == "C" else 0, name=nm, group=grp)
            if v_main:  # the cash or bank side of a receipt, payment or contra
                mk, mnm, mgrp = acct(v_main, v_main_name or v_main)
                if v_type.endswith("RECEIPT"):
                    book.post(mk, v_day, debit=float(v_amt), name=mnm, group=mgrp)
                else:
                    book.post(mk, v_day, credit=float(v_amt), name=mnm, group=mgrp)

    # purchases: the store's own, or what was received into warehouses
    place = "pi.warehouse_id is not null and (cast(:wid as uuid) is null or pi.warehouse_id = cast(:wid as uuid))" if warehouse else "pi.warehouse_id is null"
    day_expr = f"coalesce(pu.invoice_date, {IST_DAY.format(col='pu.created_at')})"
    for day, vendor_id, vendor_name, net, gst in (
        await rows(
            f"""select {day_expr} d, pu.vendor_id, v.name,
                       coalesce(sum(coalesce(pi.net_amount, pi.unit_cost * pi.quantity - coalesce(pi.discount_amount,0))),0),
                       coalesce(sum(coalesce(pi.gst_amount,0)),0)
                from purchase_items pi join purchases pu on pu.id = pi.purchase_id join vendors v on v.id = pu.vendor_id
                where pu.store_id = :sid and {place} and {day_expr} <= :upto group by 1, 2, 3"""
        )
    ).all():
        book.post("purchases", day, debit=float(net) - float(gst))
        book.post("gst_in", day, debit=gst)
        book.post(f"vendor:{vendor_id}", day, credit=net, name=f"Vendor: {vendor_name}", group="Vendors")

    if not warehouse:
        Dv = IST_DAY.format(col="vp.created_at")
        for day, vendor_id, vendor_name, amount in (
            await rows(
                f"""select {Dv}, pa.vendor_id, v.name, coalesce(sum(vp.amount),0)
                    from vendor_payments vp join payables pa on pa.id = vp.payable_id join purchases pu on pu.id = pa.purchase_id join vendors v on v.id = pa.vendor_id
                    where pu.store_id = :sid and vp.status = 'applied' and {Dv} <= :upto group by 1, 2, 3"""
            )
        ).all():
            book.post(f"vendor:{vendor_id}", day, debit=amount, name=f"Vendor: {vendor_name}", group="Vendors")
    return book


def _signed(dr: float, cr: float) -> dict:
    net = round(dr - cr, 2)
    return {"amount": abs(net), "side": "Dr" if net >= 0 else "Cr", "signed": net}


@router.get("/accounts")
async def ledger_accounts(
    store_id: uuid.UUID,
    level: str = "BRANCH",
    warehouse_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("finance.pnl.view")),
) -> list[dict]:
    """The accounts that have anything in them for this store (or its warehouses), grouped, for the account picker."""
    require_store_access(store_id, current)
    book = await build_book(db, store_id=store_id, warehouse=level.upper() == "WAREHOUSE", warehouse_id=warehouse_id, upto=date.today())
    return sorted(
        [{"key": k, "name": book.names[k][0], "group": book.names[k][1]} for k in book.days],
        key=lambda a: (a["group"], a["name"]),
    )


@router.get("")
async def ledger(
    store_id: uuid.UUID,
    date_from: date,
    date_to: date,
    level: str = "BRANCH",
    warehouse_id: uuid.UUID | None = None,
    account: str = "ALL",
    view: str = "MONTHLY",
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("finance.pnl.view")),
) -> dict:
    """One account month by month or day by day, or every account side by side, with opening and closing balances."""
    require_store_access(store_id, current)
    if date_to < date_from:
        raise HTTPException(status_code=422, detail="The end date is before the start date")
    book = await build_book(db, store_id=store_id, warehouse=level.upper() == "WAREHOUSE", warehouse_id=warehouse_id, upto=date_to)

    def split(key: str) -> tuple[float, float, dict[date, list[float]]]:
        before_dr = before_cr = 0.0
        within: dict[date, list[float]] = {}
        for day, (dr, cr) in book.days.get(key, {}).items():
            if day < date_from:
                before_dr += dr
                before_cr += cr
            else:
                within[day] = [dr, cr]
        return before_dr, before_cr, within

    if account == "ALL":
        rows, tot_dr, tot_cr = [], 0.0, 0.0
        for key in sorted(book.days, key=lambda k: (book.names[k][1], book.names[k][0])):
            b_dr, b_cr, within = split(key)
            dr = round(sum(c[0] for c in within.values()), 2)
            cr = round(sum(c[1] for c in within.values()), 2)
            if not (dr or cr or b_dr or b_cr):
                continue
            rows.append({
                "key": key, "label": book.names[key][0], "group": book.names[key][1],
                "opening": _signed(b_dr, b_cr), "debit": dr, "credit": cr, "closing": _signed(b_dr + dr, b_cr + cr),
            })
            tot_dr += dr
            tot_cr += cr
        # the same period month by month, all accounts together, for the chart
        by_month: dict[tuple[int, int], list[float]] = {}
        for key in book.days:
            for day, (dr, cr) in split(key)[2].items():
                cell = by_month.setdefault((day.year, day.month), [0.0, 0.0])
                cell[0] += dr
                cell[1] += cr
        months = [{"label": f"{MONTHS[m][:3]} {y}", "debit": round(v[0], 2), "credit": round(v[1], 2)} for (y, m), v in sorted(by_month.items())]
        return {"mode": "all", "rows": rows, "months": months, "totals": {"debit": round(tot_dr, 2), "credit": round(tot_cr, 2)}}

    if account not in book.days:
        return {"mode": "account", "account": {"key": account, "name": book.names.get(account, (account, ""))[0]}, "opening": _signed(0, 0), "rows": [], "totals": {"debit": 0, "credit": 0}}
    b_dr, b_cr, within = split(account)
    buckets: dict[tuple, list[float]] = {}
    for day, (dr, cr) in within.items():
        k = (day.year, day.month) if view.upper() == "MONTHLY" else (day.year, day.month, day.day)
        cell = buckets.setdefault(k, [0.0, 0.0])
        cell[0] += dr
        cell[1] += cr
    rows, run_dr, run_cr = [], b_dr, b_cr
    for k in sorted(buckets):
        dr, cr = round(buckets[k][0], 2), round(buckets[k][1], 2)
        run_dr += dr
        run_cr += cr
        label = f"{MONTHS[k[1]]} {k[0]}" if len(k) == 2 else date(k[0], k[1], k[2]).strftime("%d-%m-%Y")
        rows.append({"label": label, "debit": dr, "credit": cr, "closing": _signed(run_dr, run_cr)})
    return {
        "mode": "account", "account": {"key": account, "name": book.names[account][0], "group": book.names[account][1]},
        "opening": _signed(b_dr, b_cr), "rows": rows,
        "totals": {"debit": round(sum(r["debit"] for r in rows), 2), "credit": round(sum(r["credit"] for r in rows), 2)},
    }


@router.get("/purchases")
async def purchase_register(
    store_id: uuid.UUID,
    date_from: date,
    date_to: date,
    vendor_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("finance.pnl.view")),
) -> dict:
    """Every purchase bill posted for the store in the period: party by party (how many bills, how much, what is paid and what is
    still owed), the bills themselves, and the tax by GST rate. Added up from the same purchase tables the ledger uses."""
    require_store_access(store_id, current)
    if date_to < date_from:
        raise HTTPException(status_code=422, detail="The end date is before the start date")
    day = f"coalesce(pu.invoice_date, {IST_DAY.format(col='pu.created_at')})"
    params = {"sid": store_id, "d1": date_from, "d2": date_to, "vid": vendor_id}
    bills = (
        await db.execute(
            text(
                f"""select pu.id, pu.vendor_id, v.name, v.gst_number, pu.invoice_number, {day} d, pu.without_gst,
                           count(pi.id) lines,
                           coalesce(sum(pi.unit_cost * pi.quantity - coalesce(pi.discount_amount,0)),0) taxable,
                           coalesce(sum(coalesce(pi.gst_amount,0)),0) gst,
                           coalesce(sum(coalesce(pi.cess_amount,0)),0) cess,
                           coalesce(sum(coalesce(pi.surcharge_amount,0)),0) surcharge,
                           pu.total_amount,
                           (select coalesce(sum(vp.amount),0) from vendor_payments vp join payables pa on pa.id = vp.payable_id
                              where pa.purchase_id = pu.id and vp.status = 'applied') paid
                    from purchases pu join vendors v on v.id = pu.vendor_id left join purchase_items pi on pi.purchase_id = pu.id
                    where pu.store_id = :sid and {day} between :d1 and :d2 and (cast(:vid as uuid) is null or pu.vendor_id = cast(:vid as uuid))
                    group by pu.id, v.name, v.gst_number order by d desc, pu.created_at desc limit 1000"""
            ),
            params,
        )
    ).all()
    slabs = (
        await db.execute(
            text(
                f"""select coalesce(pi.gst_rate, 0) r,
                           coalesce(sum(pi.unit_cost * pi.quantity - coalesce(pi.discount_amount,0)),0) taxable,
                           coalesce(sum(coalesce(pi.gst_amount,0)),0) gst, coalesce(sum(coalesce(pi.cess_amount,0)),0) cess
                    from purchase_items pi join purchases pu on pu.id = pi.purchase_id
                    where pu.store_id = :sid and {day} between :d1 and :d2 and (cast(:vid as uuid) is null or pu.vendor_id = cast(:vid as uuid))
                    group by 1 order by 1"""
            ),
            params,
        )
    ).all()

    out_bills, parties = [], {}
    for b in bills:
        total, paid = float(b.total_amount or 0), float(b.paid or 0)
        due = round(max(total - paid, 0), 2)
        out_bills.append({
            "id": str(b.id), "vendor_id": str(b.vendor_id), "vendor": b.name, "invoice_number": b.invoice_number, "date": b.d.isoformat() if b.d else None,
            "without_gst": bool(b.without_gst), "lines": b.lines, "taxable": round(float(b.taxable), 2), "gst": round(float(b.gst), 2),
            "cess": round(float(b.cess), 2), "surcharge": round(float(b.surcharge), 2), "total": round(total, 2), "paid": round(paid, 2), "due": due,
            "status": "paid" if due <= 0.005 else ("part paid" if paid > 0 else "unpaid"),
        })
        p = parties.setdefault(str(b.vendor_id), {"vendor_id": str(b.vendor_id), "name": b.name, "gst_number": b.gst_number, "bills": 0, "taxable": 0.0, "gst": 0.0, "total": 0.0, "paid": 0.0, "due": 0.0, "last_date": None})
        p["bills"] += 1
        p["taxable"] += float(b.taxable)
        p["gst"] += float(b.gst)
        p["total"] += total
        p["paid"] += paid
        p["due"] += due
        if b.d and (p["last_date"] is None or b.d.isoformat() > p["last_date"]):
            p["last_date"] = b.d.isoformat()
    party_list = sorted(({**p, **{k: round(p[k], 2) for k in ("taxable", "gst", "total", "paid", "due")}} for p in parties.values()), key=lambda p: -p["total"])
    return {
        "bills": out_bills,
        "parties": party_list,
        "slabs": [{"rate": float(s.r), "taxable": round(float(s.taxable), 2), "gst": round(float(s.gst), 2), "cess": round(float(s.cess), 2)} for s in slabs],
        "totals": {
            "bills": len(out_bills), "parties": len(party_list),
            "taxable": round(sum(p["taxable"] for p in party_list), 2), "gst": round(sum(p["gst"] for p in party_list), 2),
            "total": round(sum(p["total"] for p in party_list), 2), "paid": round(sum(p["paid"] for p in party_list), 2), "due": round(sum(p["due"] for p in party_list), 2),
        },
    }
