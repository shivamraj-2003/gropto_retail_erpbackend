"""GSTR-1 and GSTR-3B from the bills, credit notes (returns) and purchases the system already holds.

These are WORKING FIGURES for the accountant to check and file, laid out like the return tables. They are not filed from here.

GSTR-1 (what was sold)
  B2B    invoices to customers who gave a GSTIN
  B2CL   inter-state invoices to unregistered customers worth more than 2,50,000
  B2CS   every other unregistered sale, added up by place of supply and rate (credit notes against them are taken off here)
  CDNR   credit notes (returns) against B2B invoices
  CDNUR  credit notes against B2CL invoices
  HSN    quantity and value by HSN code and rate (credit notes taken off)
  DOCS   invoice numbers issued, by till, with how many were cancelled
GSTR-3B (the monthly summary)
  3.1    tax on what was sold (credit notes taken off); nil / exempt supplies shown apart
  4      input tax credit from purchases with a tax invoice from a registered vendor. A purchase marked "without GST" (a cash
         purchase) carries no tax and no credit; a purchase from a vendor with no GSTIN gets no credit (listed as not claimed)
  6.1    tax to pay after setting the credit off the way the GST rules order it
"""

from collections import defaultdict
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.gst import GST_STATE_CODES

B2CL_LIMIT = 250000.0  # inter-state invoice value above which an unregistered sale is reported invoice by invoice


def r2(v) -> float:
    return float(Decimal(str(v or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def month_bounds(month: str) -> tuple[date, date]:
    """'2026-10' -> (first day, first day of the next month)."""
    year, mon = int(month[:4]), int(month[5:7])
    if not (1 <= mon <= 12):
        raise ValueError("The month must look like 2026-10")
    start = date(year, mon, 1)
    return start, (date(year + 1, 1, 1) if mon == 12 else date(year, mon + 1, 1))


def state_code(name: str | None) -> str:
    """GST state code for a state name ('Uttar Pradesh' -> '09'); '' when it is not known."""
    low = (name or "").strip().lower()
    return next((code for code, n in GST_STATE_CODES.items() if n.lower() == low), "")


def set_off(output: dict[str, float], credit: dict[str, float]) -> dict:
    """Pay output tax with input credit in the order the GST rules give.
    IGST credit pays IGST, then CGST, then SGST. CGST credit pays CGST, then IGST. SGST credit pays SGST, then IGST.
    CGST credit never pays SGST and SGST credit never pays CGST. Returns what is left to pay and the credit carried forward."""
    out = {k: float(output.get(k, 0)) for k in ("igst", "cgst", "sgst")}
    itc = {k: float(credit.get(k, 0)) for k in ("igst", "cgst", "sgst")}

    def use(source: str, target: str) -> None:
        take = min(itc[source], out[target])
        itc[source] -= take
        out[target] -= take

    for target in ("igst", "cgst", "sgst"):
        use("igst", target)
    use("cgst", "cgst")
    use("cgst", "igst")
    use("sgst", "sgst")
    use("sgst", "igst")
    return {"payable": {k: r2(v) for k, v in out.items()}, "carried_forward": {k: r2(v) for k, v in itc.items()}}


# --------------------------------------------------------------------------- rows from the database

_SALES = text(
    """
    select s.id::text as sale_id, s.bill_number, s.device_id::text as device_id, timezone('Asia/Kolkata', s.billed_at)::date as day,
           s.grand_total, s.place_of_supply, s.customer_gstin, st.name as store_name, st.state as store_state, st.gstin as store_gstin,
           si.id::text as item_id, si.tax_rate_snapshot as rate, si.hsn_code_snapshot as hsn, si.product_name_snapshot as name,
           si.quantity, si.taxable_value, si.cgst_amount, si.sgst_amount, si.igst_amount
    from sales s
    join sale_items si on si.sale_id = s.id
    join stores st on st.id = s.store_id
    where s.status = 'completed' and s.store_id = any(:stores)
      and timezone('Asia/Kolkata', s.billed_at)::date >= :d1 and timezone('Asia/Kolkata', s.billed_at)::date < :d2
    """
)

_NOTES = text(
    """
    select r.id::text as note_id, timezone('Asia/Kolkata', r.created_at)::date as day, s.id::text as sale_id, s.bill_number, s.customer_gstin,
           s.place_of_supply, st.state as store_state, s.grand_total as invoice_value,
           si.tax_rate_snapshot as rate, si.hsn_code_snapshot as hsn, ri.quantity, ri.refund_amount,
           (si.igst_amount > 0) as inter_state
    from returns r
    join return_items ri on ri.return_id = r.id
    join sales s on s.id = r.sale_id
    join sale_items si on si.id = ri.sale_item_id
    join stores st on st.id = r.store_id
    where r.status in ('approved', 'completed') and r.store_id = any(:stores)
      and timezone('Asia/Kolkata', r.created_at)::date >= :d1 and timezone('Asia/Kolkata', r.created_at)::date < :d2
    """
)

_DOCS = text(
    """
    select s.device_id::text as device_id, coalesce(d.code, '') as till, s.bill_number, s.status
    from sales s left join devices d on d.id = s.device_id
    where s.store_id = any(:stores)
      and timezone('Asia/Kolkata', s.billed_at)::date >= :d1 and timezone('Asia/Kolkata', s.billed_at)::date < :d2
    """
)

_PURCHASES = text(
    """
    select pu.id::text as purchase_id, pu.invoice_number, coalesce(pu.invoice_date, pu.created_at::date) as day, pu.without_gst,
           v.name as vendor, v.gst_number as vendor_gstin, st.gstin as store_gstin,
           (v.gst_number is not null and st.gstin is not null and left(v.gst_number, 2) <> left(st.gstin, 2)) as inter_state,
           pi.quantity, pi.unit_cost, coalesce(pi.discount_amount, 0) as discount,
           coalesce(pi.gst_rate, p.tax_rate, 0) as rate,
           coalesce(pi.gst_amount, round(((pi.quantity * pi.unit_cost) - coalesce(pi.discount_amount, 0)) * coalesce(pi.gst_rate, p.tax_rate, 0) / 100, 2)) as gst
    from purchases pu
    join purchase_items pi on pi.purchase_id = pu.id
    join products p on p.id = pi.product_id
    join vendors v on v.id = pu.vendor_id
    join stores st on st.id = pu.store_id
    where pu.store_id = any(:stores)
      and coalesce(pu.invoice_date, pu.created_at::date) >= :d1 and coalesce(pu.invoice_date, pu.created_at::date) < :d2
    """
)


def _tax_heads(rate_row) -> dict:
    return {"igst": r2(rate_row["igst"]), "cgst": r2(rate_row["cgst"]), "sgst": r2(rate_row["sgst"])}


async def _rows(db: AsyncSession, query, stores: list, d1: date, d2: date):
    return (await db.execute(query, {"stores": [str(s) for s in stores], "d1": d1, "d2": d2})).mappings().all()


def _note_amounts(refund: float, rate: float, inter_state: bool) -> dict:
    """A refund is GST-inclusive. Split it into taxable value and tax the way the sale was split."""
    taxable = refund / (1 + rate / 100) if rate else refund
    tax = refund - taxable
    if inter_state:
        return {"txval": r2(taxable), "igst": r2(tax), "cgst": 0.0, "sgst": 0.0}
    return {"txval": r2(taxable), "igst": 0.0, "cgst": r2(tax / 2), "sgst": r2(tax - r2(tax / 2))}


def _add(bucket: dict, **vals) -> None:
    for k, v in vals.items():
        bucket[k] = bucket.get(k, 0.0) + float(v)


# --------------------------------------------------------------------------- GSTR-1

async def gstr1(db: AsyncSession, *, stores: list, month: str) -> dict:
    d1, d2 = month_bounds(month)
    sales = await _rows(db, _SALES, stores, d1, d2)
    notes = await _rows(db, _NOTES, stores, d1, d2)
    docs = await _rows(db, _DOCS, stores, d1, d2)

    # one invoice at a time: its value and its tax by rate
    invoices: dict[str, dict] = {}
    for r in sales:
        inv = invoices.setdefault(
            r["sale_id"],
            {"inum": r["bill_number"], "idt": r["day"].isoformat(), "val": r2(r["grand_total"]), "pos": r["place_of_supply"] or r["store_state"] or "",
             "ctin": r["customer_gstin"] or "", "store_state": r["store_state"] or "", "rates": {}},
        )
        b = inv["rates"].setdefault(float(r["rate"]), {"txval": 0.0, "igst": 0.0, "cgst": 0.0, "sgst": 0.0})
        _add(b, txval=r["taxable_value"], igst=r["igst_amount"], cgst=r["cgst_amount"], sgst=r["sgst_amount"])

    def lines(inv: dict) -> list[dict]:
        return [{"rt": rt, "txval": r2(b["txval"]), "iamt": r2(b["igst"]), "camt": r2(b["cgst"]), "samt": r2(b["sgst"]), "csamt": 0.0} for rt, b in sorted(inv["rates"].items())]

    def inter(inv: dict) -> bool:
        return bool(inv["pos"] and inv["store_state"] and inv["pos"].lower() != inv["store_state"].lower())

    b2b, b2cl = [], []
    b2cs: dict[tuple, dict] = {}
    for inv in invoices.values():
        row = {"ctin": inv["ctin"], "inum": inv["inum"], "idt": inv["idt"], "val": inv["val"], "pos": state_code(inv["pos"]), "pos_name": inv["pos"], "rchrg": "N", "inv_typ": "R", "itms": lines(inv)}
        if inv["ctin"]:
            b2b.append(row)
        elif inter(inv) and inv["val"] > B2CL_LIMIT:
            row.pop("ctin")
            b2cl.append(row)
        else:
            for rt, b in inv["rates"].items():
                key = ("INTER" if inter(inv) else "INTRA", state_code(inv["pos"]), inv["pos"], rt)
                _add(b2cs.setdefault(key, {}), txval=b["txval"], igst=b["igst"], cgst=b["cgst"], sgst=b["sgst"])

    # credit notes: against B2B -> CDNR, against B2CL -> CDNUR, against anything else they come off B2CS
    cdnr: dict[str, dict] = {}
    cdnur: dict[str, dict] = {}
    hsn_notes: list[tuple] = []
    for n in notes:
        inv = invoices.get(n["sale_id"])
        value = r2(n["refund_amount"])
        parts = _note_amounts(float(n["refund_amount"]), float(n["rate"]), bool(n["inter_state"]))
        hsn_notes.append((n["hsn"] or "", float(n["rate"]), float(n["quantity"]), parts))
        target = None
        if n["customer_gstin"]:
            target = cdnr
        elif n["inter_state"] and float(n["invoice_value"]) > B2CL_LIMIT:
            target = cdnur
        if target is not None:
            note = target.setdefault(n["note_id"], {"nt_num": "CN-" + n["note_id"][:8].upper(), "nt_dt": n["day"].isoformat(), "inum": n["bill_number"], "val": 0.0, "pos": state_code(n["place_of_supply"] or n["store_state"]), "ctin": n["customer_gstin"] or "", "itms": {}})
            note["val"] += value
            b = note["itms"].setdefault(float(n["rate"]), {"txval": 0.0, "igst": 0.0, "cgst": 0.0, "sgst": 0.0})
            _add(b, **{"txval": parts["txval"], "igst": parts["igst"], "cgst": parts["cgst"], "sgst": parts["sgst"]})
        else:
            key = ("INTER" if n["inter_state"] else "INTRA", state_code(n["place_of_supply"] or n["store_state"]), n["place_of_supply"] or n["store_state"] or "", float(n["rate"]))
            _add(b2cs.setdefault(key, {}), txval=-parts["txval"], igst=-parts["igst"], cgst=-parts["cgst"], sgst=-parts["sgst"])

    def note_rows(group: dict) -> list[dict]:
        out = []
        for note in group.values():
            out.append({**{k: v for k, v in note.items() if k != "itms"}, "val": r2(note["val"]), "itms": [{"rt": rt, "txval": r2(b["txval"]), "iamt": r2(b["igst"]), "camt": r2(b["cgst"]), "samt": r2(b["sgst"]), "csamt": 0.0} for rt, b in sorted(note["itms"].items())]})
        return out

    # HSN summary: what was sold less what came back
    hsn: dict[tuple, dict] = {}
    for r in sales:
        _add(hsn.setdefault((r["hsn"] or "", float(r["rate"])), {}), qty=r["quantity"], txval=r["taxable_value"], igst=r["igst_amount"], cgst=r["cgst_amount"], sgst=r["sgst_amount"])
    for code, rate, qty, parts in hsn_notes:
        _add(hsn.setdefault((code, rate), {}), qty=-qty, txval=-parts["txval"], igst=-parts["igst"], cgst=-parts["cgst"], sgst=-parts["sgst"])

    # documents issued: the number range of each till's invoices
    by_till: dict[str, dict] = {}
    for d in docs:
        t = by_till.setdefault(d["device_id"], {"till": d["till"], "numbers": [], "cancelled": 0})
        t["numbers"].append(d["bill_number"])
        if d["status"] != "completed":
            t["cancelled"] += 1
    docs_out = [
        {"till": t["till"], "from": min(t["numbers"]), "to": max(t["numbers"]), "total": len(t["numbers"]), "cancelled": t["cancelled"], "net_issued": len(t["numbers"]) - t["cancelled"]}
        for t in by_till.values()
    ]

    b2cs_rows = [
        {"sply_ty": k[0], "pos": k[1], "pos_name": k[2], "rt": k[3], "txval": r2(v.get("txval")), "iamt": r2(v.get("igst")), "camt": r2(v.get("cgst")), "samt": r2(v.get("sgst")), "csamt": 0.0}
        for k, v in sorted(b2cs.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][3]))
    ]
    hsn_rows = [
        {"hsn_sc": k[0], "rt": k[1], "qty": round(v.get("qty", 0), 3), "txval": r2(v.get("txval")), "iamt": r2(v.get("igst")), "camt": r2(v.get("cgst")), "samt": r2(v.get("sgst"))}
        for k, v in sorted(hsn.items())
    ]
    totals = {
        "invoices": len(invoices),
        "taxable": r2(sum(r["txval"] for r in b2cs_rows) + sum(i["txval"] for row in b2b + b2cl for i in row["itms"])),
        "igst": r2(sum(r["iamt"] for r in b2cs_rows) + sum(i["iamt"] for row in b2b + b2cl for i in row["itms"])),
        "cgst": r2(sum(r["camt"] for r in b2cs_rows) + sum(i["camt"] for row in b2b + b2cl for i in row["itms"])),
        "sgst": r2(sum(r["samt"] for r in b2cs_rows) + sum(i["samt"] for row in b2b + b2cl for i in row["itms"])),
    }
    return {
        "period": month, "from": d1.isoformat(), "to_exclusive": d2.isoformat(),
        "b2b": b2b, "b2cl": b2cl, "b2cs": b2cs_rows, "cdnr": note_rows(cdnr), "cdnur": note_rows(cdnur), "hsn": hsn_rows, "docs": docs_out,
        "totals": totals,
        "notes_total": r2(sum(float(n["refund_amount"]) for n in notes)),
        "warnings": _gstr1_warnings(invoices, b2b, hsn_rows),
    }


def _gstr1_warnings(invoices: dict, b2b: list, hsn_rows: list) -> list[str]:
    out = []
    missing = sum(1 for r in hsn_rows if not r["hsn_sc"])
    if missing:
        out.append(f"{missing} HSN line(s) have no HSN code. Add the HSN to those products before filing")
    bad = [r["ctin"] for r in b2b if len(r["ctin"]) != 15]
    if bad:
        out.append(f"{len(bad)} B2B invoice(s) carry a customer GSTIN that is not 15 characters")
    if not invoices:
        out.append("There are no bills in this month")
    return out


# --------------------------------------------------------------------------- GSTR-3B

async def gstr3b(db: AsyncSession, *, stores: list, month: str) -> dict:
    d1, d2 = month_bounds(month)
    one = await gstr1(db, stores=stores, month=month)

    # outward: everything in GSTR-1 (B2CS already has the credit notes off) less credit notes on B2B / B2CL
    taxable_rows = {"txval": 0.0, "igst": 0.0, "cgst": 0.0, "sgst": 0.0}
    nil_rows = {"txval": 0.0}
    inter_unreg: dict[str, dict] = defaultdict(lambda: {"txval": 0.0, "igst": 0.0})

    def put(rate: float, txval: float, igst: float, cgst: float, sgst: float, *, sign: int = 1) -> None:
        if rate > 0:
            _add(taxable_rows, txval=sign * txval, igst=sign * igst, cgst=sign * cgst, sgst=sign * sgst)
        else:
            _add(nil_rows, txval=sign * txval)

    for row in one["b2b"] + one["b2cl"]:
        for i in row["itms"]:
            put(i["rt"], i["txval"], i["iamt"], i["camt"], i["samt"])
    for row in one["b2cs"]:
        put(row["rt"], row["txval"], row["iamt"], row["camt"], row["samt"])
        if row["sply_ty"] == "INTER" and row["rt"] > 0:
            _add(inter_unreg[row["pos_name"] or row["pos"]], txval=row["txval"], igst=row["iamt"])
    for row in one["cdnr"] + one["cdnur"]:
        for i in row["itms"]:
            put(i["rt"], i["txval"], i["iamt"], i["camt"], i["samt"], sign=-1)

    # input tax credit from purchases
    purchases = await _rows(db, _PURCHASES, stores, d1, d2)
    itc = {"igst": 0.0, "cgst": 0.0, "sgst": 0.0}
    not_claimed = {"igst": 0.0, "cgst": 0.0, "sgst": 0.0}
    inward_exempt = {"inter": 0.0, "intra": 0.0}
    cash_purchases = 0.0
    bills_with_credit: set[str] = set()
    for p in purchases:
        value = float(p["quantity"]) * float(p["unit_cost"]) - float(p["discount"])
        gst = float(p["gst"] or 0)
        if p["without_gst"]:
            cash_purchases += value
            _add(inward_exempt, **{"inter" if p["inter_state"] else "intra": value})
            continue
        if float(p["rate"]) == 0 or gst == 0:
            _add(inward_exempt, **{"inter" if p["inter_state"] else "intra": value})
            continue
        heads = {"igst": gst} if p["inter_state"] else {"cgst": r2(gst / 2), "sgst": r2(gst - r2(gst / 2))}
        if p["vendor_gstin"]:
            _add(itc, **heads)
            bills_with_credit.add(p["purchase_id"])
        else:
            _add(not_claimed, **heads)

    output = {"igst": r2(taxable_rows["igst"]), "cgst": r2(taxable_rows["cgst"]), "sgst": r2(taxable_rows["sgst"])}
    settled = set_off(output, itc)
    warnings = list(one["warnings"])
    if sum(not_claimed.values()) > 0:
        warnings.append(f"Tax of ₹{r2(sum(not_claimed.values())):,.2f} on purchases from vendors with no GSTIN is not claimed as credit. Add the vendor's GSTIN if they are registered")
    return {
        "period": month,
        "outward_taxable": {k: r2(v) for k, v in taxable_rows.items()},  # 3.1(a)
        "outward_nil_exempt": {"txval": r2(nil_rows["txval"])},  # 3.1(c)
        "inter_state_unregistered": [{"pos": k, "txval": r2(v["txval"]), "igst": r2(v["igst"])} for k, v in sorted(inter_unreg.items())],  # 3.2
        "itc_available": {k: r2(v) for k, v in itc.items()},  # 4(A)(5)
        "itc_not_claimed": {k: r2(v) for k, v in not_claimed.items()},
        "inward_nil_exempt": {k: r2(v) for k, v in inward_exempt.items()},  # 5
        "cash_purchases_without_gst": r2(cash_purchases),
        "tax_payable_before_credit": output,
        "tax_to_pay": settled["payable"],  # 6.1
        "itc_carried_forward": settled["carried_forward"],
        "purchase_bills_with_credit": len(bills_with_credit),
        "warnings": warnings,
    }
