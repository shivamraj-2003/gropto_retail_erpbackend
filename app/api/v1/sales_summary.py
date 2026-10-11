"""Sales totals by day, store and counter (till), for the Sales screen and its Excel download.

Counted: completed bills only, by the shop's local date (India time). Always limited to the stores the caller may see.
"""

import io
import uuid
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from openpyxl import Workbook
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission, require_store_access
from app.core.database import get_reporting_db

router = APIRouter(prefix="/sales-summary", tags=["sales-summary"])

GROUPS = {"store_day", "counter_day", "store"}

_BASE = """
    with pay as (
        select sale_id,
               sum(amount) filter (where mode = 'cash') as cash,
               sum(amount) filter (where mode = 'card') as card,
               sum(amount) filter (where mode = 'upi') as upi,
               sum(amount) filter (where mode not in ('cash', 'card', 'upi')) as other
        from public.payments group by sale_id
    ),
    b as (
        select s.id, s.store_id, s.device_id, (s.billed_at at time zone 'Asia/Kolkata')::date as day,
               s.grand_total, s.discount_total, s.tax_total,
               coalesce(p.cash, 0) as cash, coalesce(p.card, 0) as card, coalesce(p.upi, 0) as upi, coalesce(p.other, 0) as other
        from public.sales s left join pay p on p.sale_id = s.id
        where s.status = 'completed'
          and (s.billed_at at time zone 'Asia/Kolkata')::date between :d_from and :d_to
          {store_filter}
    )
"""

_SELECTS = {
    "store_day": """
        select b.day, st.name as store, '' as counter, count(*) as bills, sum(b.grand_total) as gross,
               sum(b.discount_total) as discount, sum(b.tax_total) as tax,
               sum(b.cash) as cash, sum(b.card) as card, sum(b.upi) as upi, sum(b.other) as other
        from b join public.stores st on st.id = b.store_id
        group by b.day, st.name order by b.day desc, st.name""",
    "counter_day": """
        select b.day, st.name as store, d.code as counter, count(*) as bills, sum(b.grand_total) as gross,
               sum(b.discount_total) as discount, sum(b.tax_total) as tax,
               sum(b.cash) as cash, sum(b.card) as card, sum(b.upi) as upi, sum(b.other) as other
        from b join public.stores st on st.id = b.store_id join public.devices d on d.id = b.device_id
        group by b.day, st.name, d.code order by b.day desc, st.name, d.code""",
    "store": """
        select null::date as day, st.name as store, '' as counter, count(*) as bills, sum(b.grand_total) as gross,
               sum(b.discount_total) as discount, sum(b.tax_total) as tax,
               sum(b.cash) as cash, sum(b.card) as card, sum(b.upi) as upi, sum(b.other) as other
        from b join public.stores st on st.id = b.store_id
        group by st.name order by sum(b.grand_total) desc""",
}

_COLS = ["bills", "gross", "discount", "tax", "cash", "card", "upi", "other"]


async def _rows(
    db: AsyncSession, current: CurrentUser, group: str, store_id: uuid.UUID | None, d_from: date, d_to: date
) -> list[dict]:
    if group not in GROUPS:
        raise HTTPException(status_code=400, detail="Unknown grouping")
    if d_to < d_from:
        raise HTTPException(status_code=400, detail="The end date is before the start date")
    if (d_to - d_from).days > 366:
        raise HTTPException(status_code=400, detail="Choose a period of up to one year")
    params: dict = {"d_from": d_from, "d_to": d_to}
    store_filter = ""
    if store_id is not None:
        require_store_access(store_id, current)
        store_filter = "and s.store_id = :sid"
        params["sid"] = store_id
    elif not current.sees_all_stores():
        store_filter = "and s.store_id = any(:store_ids)"
        params["store_ids"] = list(current.store_ids)
    sql = _BASE.replace("{store_filter}", store_filter) + _SELECTS[group]
    result = (await db.execute(text(sql), params)).mappings().all()
    out = []
    for r in result:
        row = {"day": r["day"].isoformat() if r["day"] else None, "store": r["store"], "counter": r["counter"]}
        for c in _COLS:
            row[c] = int(r[c]) if c == "bills" else float(r[c] or 0)
        out.append(row)
    return out


def _totals(rows: list[dict]) -> dict:
    t = {c: round(sum(r[c] for r in rows), 2) for c in _COLS}
    t["bills"] = int(t["bills"])
    return t


def _period(date_from: date | None, date_to: date | None) -> tuple[date, date]:
    end = date_to or date.today()
    return (date_from or end - timedelta(days=29), end)


@router.get("")
async def sales_summary(
    group: str = "store_day",
    store_id: uuid.UUID | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    db: AsyncSession = Depends(get_reporting_db),
    current: CurrentUser = Depends(require_permission("reports.sales.export")),
) -> dict:
    d_from, d_to = _period(date_from, date_to)
    rows = await _rows(db, current, group, store_id, d_from, d_to)
    return {"group": group, "date_from": d_from.isoformat(), "date_to": d_to.isoformat(), "rows": rows, "totals": _totals(rows)}


@router.get("/export")
async def sales_summary_export(
    group: str = "store_day",
    store_id: uuid.UUID | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    db: AsyncSession = Depends(get_reporting_db),
    current: CurrentUser = Depends(require_permission("reports.sales.export")),
) -> StreamingResponse:
    d_from, d_to = _period(date_from, date_to)
    rows = await _rows(db, current, group, store_id, d_from, d_to)
    wb = Workbook()
    ws = wb.active
    ws.title = "Sales"
    ws.append(["Date", "Store", "Counter", "Bills", "Total sales", "Discount", "GST", "Cash", "Card", "UPI", "Other"])
    for r in rows:
        ws.append([r["day"] or f"{d_from} to {d_to}", r["store"], r["counter"], r["bills"], r["gross"], r["discount"], r["tax"], r["cash"], r["card"], r["upi"], r["other"]])
    t = _totals(rows)
    ws.append(["Total", "", "", t["bills"], t["gross"], t["discount"], t["tax"], t["cash"], t["card"], t["upi"], t["other"]])
    for col in "ABCDEFGHIJK":
        ws.column_dimensions[col].width = 16
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="sales-{group}.xlsx"'},
    )
