"""Profit & Loss account and Balance Sheet, laid out the way shop accountants read them (two sides, grouped heads). Both are worked
out from the same real records as the ledger (see ledger.py); stock is valued at each product's cost price from the stock movements."""

import re
import uuid
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission, require_store_access
from app.api.v1.ledger import IST_DAY, Book, build_book
from app.core.database import get_db
from app.services.purchase_bill import PURCHASE_ACCOUNT, inter_state_sql

router = APIRouter(prefix="/finance-advanced/statements", tags=["statements"])

# An account the shop named itself in a voucher is placed by the words in its name; the first rule that matches wins.
CUSTOM_RULES: list[tuple[str, str]] = [
    (r"capital|drawing", "capital"),
    (r"fixed deposit|\bfd\b", "investment"),
    (r"\bbank\b(?!.*(charge|loan|interest))", "current_assets"),
    (r"fixed deposit|\bfd\b|investment|mutual|shares", "investment"),
    (r"\bloans?\b|overdraft|\bod\b|borrow|\bcc limit\b", "loan"),
    (r"income|received|commission earned|scheme", "income"),
    (r"freight|carriage|cartage|loading|unloading|labou?r|direct", "direct"),
    (r"furniture|fixture|computer|printer|machine|vehicle|equipment|fixed asset|building|interior|\bac\b|refrigerator|freezer", "fixed"),
    (r"expense|rent|salary|wage|charge|electric|power|fee|repair|maintenance|travel|tea|stationery|discount allowed|interest paid", "indirect"),
    (r"misc", "misc"),
]

BS_GROUPS = [
    ("capital", "CAPITAL ACCOUNT"),
    ("current_assets", "CURRENT ASSETS"),
    ("current_liabilities", "CURRENT LIABILITIES"),
    ("fixed", "FIXED ASSETS"),
    ("investment", "INVESTMENTS"),
    ("loan", "LOANS (Liability)"),
    ("misc", "MISC. EXPENSES (Asset)"),
    ("suspense", "SUSPENSE ACCOUNT (Temporary A/cs)"),
]


def custom_head(name: str) -> str:
    low = name.lower()
    for pattern, head in CUSTOM_RULES:
        if re.search(pattern, low):
            return head
    return "suspense"


def head_of(key: str, name: str, chosen: str | None = None) -> str:
    """Which head an account belongs to: a P&L head (sales, returns, purchases, direct, indirect, income) or a balance sheet group."""
    if key in ("sales", "returns", "purchases"):
        return key
    if key.startswith("expense:"):
        return "indirect"
    if key.startswith("vendor:"):
        return "current_liabilities"
    if key in ("gst_out", "gst_rcm"):
        return "current_liabilities"
    if key in ("cash", "bank", "upi", "card", "other_tender", "udhaar", "gst_in"):
        return "current_assets"
    if key.startswith("custom:"):
        return chosen or custom_head(name)
    return "suspense"


def movements(book: Book, date_from: date | None, date_to: date) -> dict[str, float]:
    """Net debit minus credit of every account within the dates."""
    out: dict[str, float] = {}
    for key, days in book.days.items():
        total = sum(dr - cr for day, (dr, cr) in days.items() if day <= date_to and (date_from is None or day >= date_from))
        if abs(total) > 0.004:
            out[key] = round(total, 2)
    return out


async def stock_value(db: AsyncSession, store_id: uuid.UUID, upto: date) -> float:
    """The store's stock at the end of a day, at cost price (a product that shows negative stock counts as nothing)."""
    day = IST_DAY.format(col="m.created_at")
    value = await db.scalar(
        text(
            f"""select coalesce(sum(greatest(q.qty, 0) * coalesce(p.purchase_price, 0)), 0)
                from (select m.product_id, sum(m.delta) qty from inventory_movements m
                      where m.store_id = :sid and {day} <= :upto group by m.product_id) q
                join products p on p.id = q.product_id"""
        ),
        {"sid": store_id, "upto": upto},
    )
    return round(float(value or 0), 2)


async def opening_imports(db: AsyncSession, store_id: uuid.UUID, date_from: date | None, date_to: date) -> float:
    """Stock loaded as opening stock within the dates (an import), at cost: it is stock the shop already had, not something earned."""
    day = IST_DAY.format(col="m.created_at")
    value = await db.scalar(
        text(
            f"""select coalesce(sum(m.delta * coalesce(p.purchase_price, 0)), 0)
                from inventory_movements m join products p on p.id = m.product_id
                where m.store_id = :sid and m.reason_code = 'opening_stock' and {day} <= :upto and (cast(:since as date) is null or {day} >= :since)"""
        ),
        {"sid": store_id, "since": date_from, "upto": date_to},
    )
    return round(float(value or 0), 2)


def profit_and_loss(book: Book, date_from: date | None, date_to: date, opening_stock: float, closing_stock: float) -> dict:
    moved = movements(book, date_from, date_to)
    heads: dict[str, list[dict]] = {}
    for key, net in moved.items():
        name = book.names[key][0]
        head = head_of(key, name, book.heads.get(key))
        if head in ("sales", "returns", "purchases", "direct", "indirect", "income"):
            # income heads are credit balances, cost heads are debit balances
            amount = -net if head in ("sales", "income") else net
            heads.setdefault(head, []).append({"name": name, "amount": round(amount, 2)})

    def total(head: str) -> float:
        return round(sum(a["amount"] for a in heads.get(head, [])), 2)

    sales, returns, purchases, direct, indirect, income = (total(h) for h in ("sales", "returns", "purchases", "direct", "indirect", "income"))
    gross = round(sales + closing_stock - opening_stock - purchases - returns - direct, 2)
    net = round(gross + income - indirect, 2)

    def pct(x: float) -> float:
        return round(abs(x) * 100 / sales, 2) if sales else 0.0

    def line(label: str, amount: float, head: str | None = None) -> dict:
        return {"label": label, "amount": round(amount, 2), "accounts": sorted(heads.get(head, []), key=lambda a: -abs(a["amount"])) if head else []}

    trading_dr = [line("Opening Stock", opening_stock), line("Purchase A/c", purchases, "purchases"), line("Sales Returns", returns, "returns"), line("Expenses (Direct)", direct, "direct")]
    trading_cr = [line("Sales A/c", sales, "sales"), line("Closing Stock", closing_stock)]
    trading_total = round(max(sales + closing_stock, opening_stock + purchases + returns + direct), 2)
    pl_dr = [line("Expenses (Indirect)", indirect, "indirect")]
    pl_cr = [line("Income (Revenue)", income, "income")]
    pl_total = round(max(indirect + max(-gross, 0), income + max(gross, 0)), 2)
    return {
        "trading": {"dr": trading_dr, "cr": trading_cr, "total": trading_total, "gross": gross, "gross_pct": pct(gross)},
        "pl": {"dr": pl_dr, "cr": pl_cr, "total": pl_total, "net": net, "net_pct": pct(net)},
    }


def balance_sheet(book: Book, upto: date, closing_stock: float, opening_stock: float = 0.0) -> dict:
    moved = movements(book, None, upto)
    groups: dict[str, list[dict]] = {g: [] for g, _ in BS_GROUPS}
    for key, net in moved.items():
        name = book.names[key][0]
        head = head_of(key, name, book.heads.get(key))
        if head not in groups:
            continue  # P&L heads are in the net profit or loss
        # a vendor who was paid in advance is an asset; an overdrawn cash or bank account is a liability
        if head == "current_liabilities" and net > 0:
            head = "current_assets"
        elif head == "current_assets" and net < 0:
            head = "current_liabilities"
        groups[head].append({"name": name, "amount": net})
    if closing_stock:
        groups["current_assets"].append({"name": "Closing Stock (at cost)", "amount": closing_stock})

    # the profit or loss since the books began
    net_profit = profit_and_loss(book, None, upto, opening_stock, closing_stock)["pl"]["net"]

    rows, assets, liabilities = [], 0.0, 0.0
    for key, label in BS_GROUPS:
        accounts = sorted(groups[key], key=lambda a: -abs(a["amount"]))
        signed = round(sum(a["amount"] for a in accounts), 2)
        asset = signed if signed > 0 else 0.0
        liability = -signed if signed < 0 else 0.0
        assets += asset
        liabilities += liability
        rows.append({"key": key, "label": label, "asset": round(asset, 2), "liability": round(liability, 2),
                     "accounts": [{"name": a["name"], "amount": abs(a["amount"]), "side": "Dr" if a["amount"] >= 0 else "Cr"} for a in accounts]})
    if net_profit >= 0:
        liabilities += net_profit
    else:
        assets += -net_profit
    # what the two sides do not agree by: opening balances that were never entered (stock, capital, cash brought forward)
    difference = round(assets - liabilities, 2)
    total = round(max(assets, liabilities), 2)
    return {
        "rows": rows,
        "net_profit": net_profit,
        "difference": {"amount": abs(difference), "side": "Cr" if difference > 0 else "Dr"},
        "total": total,
    }


SUPPLIES = (("local", "Local (within state)"), ("central", "Central (outside state)"))
_AMOUNTS = ("taxable", "cgst", "sgst", "igst", "tax")


def _add_up(rows: list[dict]) -> dict:
    return {k: round(sum(r[k] for r in rows), 2) for k in _AMOUNTS}


def by_supply(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Rows keyed by (supply, rate) into: one row per GST rate, and Local / Central, each with its totals and its own rates.
    Goods bought with no tax invoice (rows marked `without_gst`) count in the totals but are no GST rate's: they are given apart."""
    rates = [{"rate": rate, **_add_up([r for r in rows if r["rate"] == rate])} for rate in sorted({r["rate"] for r in rows})]
    supplies = []
    for key, label in SUPPLIES:
        mine = sorted((r for r in rows if r["supply"] == key), key=lambda r: r["rate"])
        billed = [r for r in mine if not r.get("without_gst")]
        supplies.append({
            "key": key, "label": label, **_add_up(mine),
            "without_gst": round(sum(r["taxable"] for r in mine if r.get("without_gst")), 2),
            "rates": [{k: v for k, v in r.items() if k not in ("supply", "without_gst")} for r in billed],
        })
    return rates, supplies


async def rate_detail(db: AsyncSession, store_id: uuid.UUID, date_from: date, date_to: date) -> dict:
    """Sales and purchases of the period by GST rate, each also split into Local (CGST + SGST) and Central (IGST, another state):
    the value before GST and the GST on it."""
    p = {"sid": store_id, "d1": date_from, "d2": date_to}
    sale_day = IST_DAY.format(col="s.billed_at")
    sales = await db.execute(
        text(
            f"""select si.tax_rate_snapshot rate, (coalesce(si.igst_amount,0) <> 0) central, coalesce(sum(si.taxable_value),0) taxable,
                       coalesce(sum(si.cgst_amount),0) cgst, coalesce(sum(si.sgst_amount),0) sgst, coalesce(sum(si.igst_amount),0) igst
                from sales s join sale_items si on si.sale_id = s.id
                where s.store_id = :sid and s.status = 'completed' and {sale_day} between :d1 and :d2 group by 1, 2"""
        ),
        p,
    )
    out_sales = [
        {"rate": float(r.rate), "supply": "central" if r.central else "local", "taxable": round(float(r.taxable), 2), "cgst": round(float(r.cgst), 2), "sgst": round(float(r.sgst), 2),
         "igst": round(float(r.igst), 2), "tax": round(float(r.cgst + r.sgst + r.igst), 2)}
        for r in sales.all()
    ]
    pur_day = f"coalesce(pu.invoice_date, {IST_DAY.format(col='pu.created_at')})"
    # Central = between states, as ticked on the bill (else by the two GST numbers; a vendor with none counts as local)
    purchases = await db.execute(
        text(
            f"""select coalesce(pi.gst_rate,0) rate, {inter_state_sql()} central, pu.without_gst,
                       coalesce(sum({PURCHASE_ACCOUNT}),0) taxable, coalesce(sum(coalesce(pi.gst_amount,0)),0) gst
                from purchase_items pi join purchases pu on pu.id = pi.purchase_id join vendors v on v.id = pu.vendor_id join stores st on st.id = pu.store_id
                where pu.store_id = :sid and pi.warehouse_id is null and {pur_day} between :d1 and :d2 group by 1, 2, 3"""
        ),
        p,
    )
    out_pur = []
    for r in purchases.all():
        gst = round(float(r.gst), 2)
        igst = gst if r.central else 0.0
        half = round((gst - igst) / 2, 2)
        out_pur.append({"rate": float(r.rate), "supply": "central" if r.central else "local", "without_gst": bool(r.without_gst), "taxable": round(float(r.taxable), 2),
                        "cgst": half, "sgst": round(gst - igst - half, 2), "igst": igst, "tax": gst})
    sale_rates, sale_supplies = by_supply(out_sales)
    purchase_rates, purchase_supplies = by_supply(out_pur)
    return {"sales": sale_rates, "purchases": purchase_rates, "sales_supplies": sale_supplies, "purchase_supplies": purchase_supplies}


@router.get("")
async def statements(
    store_id: uuid.UUID,
    date_from: date,
    date_to: date,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("finance.pnl.view")),
) -> dict:
    require_store_access(store_id, current)
    if date_to < date_from:
        raise HTTPException(status_code=422, detail="The end date is before the start date")
    book = await build_book(db, store_id=store_id, warehouse=False, warehouse_id=None, upto=date_to)
    # opening stock: what was there before the period, plus any opening stock loaded during it
    opening = round(await stock_value(db, store_id, date_from - timedelta(days=1)) + await opening_imports(db, store_id, date_from, date_to), 2)
    first_stock = await opening_imports(db, store_id, None, date_to)  # all opening stock ever loaded, for the balance sheet's profit
    closing = await stock_value(db, store_id, date_to)
    head = (
        await db.execute(
            text("select s.name store, s.code, s.city, c.name company from stores s left join companies c on c.id = s.company_id where s.id = :sid"),
            {"sid": store_id},
        )
    ).first()
    pl = profit_and_loss(book, date_from, date_to, opening, closing)
    rates = await rate_detail(db, store_id, date_from, date_to)
    for line in pl["trading"]["dr"] + pl["trading"]["cr"]:
        if line["label"] == "Sales A/c":
            line["rates"], line["supplies"] = rates["sales"], rates["sales_supplies"]
        elif line["label"] == "Purchase A/c":
            line["rates"], line["supplies"] = rates["purchases"], rates["purchase_supplies"]
    return {
        "company": (head.company if head else None) or (head.store if head else ""),
        "store": f"{head.code} {head.store}" + (f", {head.city}" if head and head.city else "") if head else "",
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "profit_and_loss": pl,
        "balance_sheet": balance_sheet(book, date_to, closing, first_stock),
    }
