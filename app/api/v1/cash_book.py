"""Cash book: the cash that came into and went out of the tills, day by day, with what each movement was for.

Cash sales come from the bills; "cash in" and "cash out" are the movements the cashiers record on the Daily Cash screen
(petty cash added, an expense or payout taken). Dates are India dates. Only completed bills count.
"""

import uuid
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission, require_store_access
from app.core.database import get_reporting_db

router = APIRouter(prefix="/cash-book", tags=["cash-book"])

_SALES = """
    select (s.billed_at at time zone 'Asia/Kolkata')::date as day, coalesce(sum(p.amount), 0) as cash_sales
    from public.payments p join public.sales s on s.id = p.sale_id
    where p.mode = 'cash' and s.status = 'completed'
      and (s.billed_at at time zone 'Asia/Kolkata')::date between :d_from and :d_to
      {store_filter_s}
    group by 1
"""

_MOVEMENTS = """
    select m.created_at, (m.created_at at time zone 'Asia/Kolkata')::date as day,
           to_char(m.created_at at time zone 'Asia/Kolkata', 'HH24:MI') as at_time,
           m.direction, m.amount, m.reason,
           st.name as store, d.code as till, u.full_name as cashier
    from public.cash_movements m
    join public.cashier_shifts sh on sh.id = m.shift_id
    join public.stores st on st.id = sh.store_id
    join public.devices d on d.id = sh.device_id
    join public.users u on u.id = sh.cashier_id
    where (m.created_at at time zone 'Asia/Kolkata')::date between :d_from and :d_to
      {store_filter_sh}
    order by m.created_at desc
"""


@router.get("")
async def cash_book(
    store_id: uuid.UUID | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    db: AsyncSession = Depends(get_reporting_db),
    current: CurrentUser = Depends(require_permission("reports.sales.export")),
) -> dict:
    end = date_to or date.today()
    start = date_from or end - timedelta(days=29)
    if end < start:
        raise HTTPException(status_code=400, detail="The end date is before the start date")
    if (end - start).days > 366:
        raise HTTPException(status_code=400, detail="Choose a period of up to one year")

    params: dict = {"d_from": start, "d_to": end}
    sales_filter = movement_filter = ""
    if store_id is not None:
        require_store_access(store_id, current)
        sales_filter, movement_filter, params["sid"] = "and s.store_id = :sid", "and sh.store_id = :sid", store_id
    elif not current.sees_all_stores():
        sales_filter, movement_filter = "and s.store_id = any(:store_ids)", "and sh.store_id = any(:store_ids)"
        params["store_ids"] = list(current.store_ids)

    sales = (await db.execute(text(_SALES.replace("{store_filter_s}", sales_filter)), params)).mappings().all()
    moves = (await db.execute(text(_MOVEMENTS.replace("{store_filter_sh}", movement_filter)), params)).mappings().all()

    days: dict[str, dict] = {}

    def day(key: date) -> dict:
        k = key.isoformat()
        return days.setdefault(k, {"date": k, "cash_sales": 0.0, "cash_in": 0.0, "cash_out": 0.0, "movements": []})

    for r in sales:
        day(r["day"])["cash_sales"] = round(float(r["cash_sales"]), 2)
    by_reason: dict[tuple[str, str], dict] = {}
    for r in moves:
        amount = round(float(r["amount"]), 2)
        d = day(r["day"])
        d["cash_in" if r["direction"] == "in" else "cash_out"] = round(d["cash_in" if r["direction"] == "in" else "cash_out"] + amount, 2)
        d["movements"].append(
            {
                "time": r["at_time"],
                "direction": r["direction"],
                "amount": amount,
                "reason": r["reason"],
                "store": r["store"],
                "till": r["till"],
                "cashier": r["cashier"],
            }
        )
        key = (r["direction"], (r["reason"] or "").strip().lower())
        g = by_reason.setdefault(key, {"direction": r["direction"], "reason": (r["reason"] or "").strip(), "count": 0, "total": 0.0})
        g["count"] += 1
        g["total"] = round(g["total"] + amount, 2)

    rows = sorted(days.values(), key=lambda d: d["date"], reverse=True)
    for d in rows:
        d["net"] = round(d["cash_sales"] + d["cash_in"] - d["cash_out"], 2)
    totals = {k: round(sum(d[k] for d in rows), 2) for k in ("cash_sales", "cash_in", "cash_out", "net")}
    return {
        "date_from": start.isoformat(),
        "date_to": end.isoformat(),
        "days": rows,
        "totals": totals,
        "by_reason": sorted(by_reason.values(), key=lambda g: (g["direction"], -g["total"])),
    }
