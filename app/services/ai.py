"""The AI never sees the database — it sees a compact JSON summary that SQL has
already computed, and writes prose about it. Figures always render whether or not
the LLM call succeeds; the AI is an enhancement layer, never a dependency."""

import hashlib
import json
import uuid
from datetime import date, datetime, timedelta, timezone

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.models import AiCallCounter, AiInsightCache

CACHE_TTL_MINUTES = 30


async def build_kpi_payload(db: AsyncSession, store_id: uuid.UUID) -> dict:
    sales_today = (
        await db.execute(
            text(
                """
                select coalesce(sum(grand_total), 0) as revenue, count(*) as bills
                from sales
                where store_id = :store_id and billed_at::date = current_date and status = 'completed'
                """
            ),
            {"store_id": str(store_id)},
        )
    ).one()

    trailing_avg = (
        await db.execute(
            text(
                """
                select coalesce(avg(daily_total), 0) from (
                    select date_trunc('day', billed_at) d, sum(grand_total) daily_total
                    from sales
                    where store_id = :store_id and billed_at > current_date - interval '14 days'
                      and billed_at < current_date and status = 'completed'
                    group by 1
                ) t
                """
            ),
            {"store_id": str(store_id)},
        )
    ).scalar_one()

    top_movers = (
        await db.execute(
            text(
                """
                select p.name, sum(si.quantity) qty, sum(si.line_total) value
                from sale_items si
                join sales s on s.id = si.sale_id
                join products p on p.id = si.product_id
                where s.store_id = :store_id and s.billed_at > now() - interval '7 days'
                group by p.name order by value desc limit 5
                """
            ),
            {"store_id": str(store_id)},
        )
    ).all()

    reorder_alerts = (
        await db.execute(
            text(
                """
                select p.name, ib.quantity
                from inventory_balances ib
                join products p on p.id = ib.product_id
                where ib.store_id = :store_id and ib.quantity < 10 and p.is_active
                order by ib.quantity asc limit 10
                """
            ),
            {"store_id": str(store_id)},
        )
    ).all()

    return {
        "sales_today_revenue": float(sales_today.revenue),
        "sales_today_bills": int(sales_today.bills),
        "trailing_14d_daily_avg_revenue": float(trailing_avg or 0),
        "top_movers_7d": [{"name": r.name, "qty": float(r.qty), "value": float(r.value)} for r in top_movers],
        "low_stock_alerts": [{"name": r.name, "quantity": float(r.quantity)} for r in reorder_alerts],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _cache_key(store_id: uuid.UUID, payload: dict, question: str | None) -> str:
    blob = json.dumps({"store": str(store_id), "payload": payload, "question": question}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


async def _under_daily_cap(db: AsyncSession, store_id: uuid.UUID) -> bool:
    today = date.today()
    counter = await db.get(AiCallCounter, (store_id, today))
    return counter is None or counter.call_count < settings.ai_daily_call_cap


async def _increment_counter(db: AsyncSession, store_id: uuid.UUID) -> None:
    today = date.today()
    counter = await db.get(AiCallCounter, (store_id, today))
    if counter is None:
        counter = AiCallCounter(store_id=store_id, call_date=today, call_count=1)
        db.add(counter)
    else:
        counter.call_count += 1


async def _call_llm(payload: dict, question: str | None) -> str | None:
    if not settings.ai_provider_api_key or not settings.ai_provider_base_url:
        return None
    prompt = (
        "You are a retail operations analyst. Given this store KPI snapshot as JSON, "
        "write a concise 3-4 sentence narrative highlighting what matters most. "
        f"{'Then answer this question: ' + question if question else ''}\n\n{json.dumps(payload, default=str)}"
    )
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                settings.ai_provider_base_url,
                headers={"Authorization": f"Bearer {settings.ai_provider_api_key}"},
                json={"messages": [{"role": "user", "content": prompt}], "max_tokens": 300},
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("content") or data.get("choices", [{}])[0].get("message", {}).get("content")
    except (httpx.HTTPError, KeyError, IndexError):
        return None  # degrade to facts-only; the AI is never a dependency


async def get_insight(db: AsyncSession, *, store_id: uuid.UUID, question: str | None) -> dict:
    payload = await build_kpi_payload(db, store_id)
    key = _cache_key(store_id, payload, question)

    cached = await db.get(AiInsightCache, key)
    now = datetime.now(timezone.utc)
    if cached and cached.expires_at.replace(tzinfo=timezone.utc) > now:
        return {"facts": cached.payload, "narrative": cached.narrative, "generated_at": cached.created_at, "from_cache": True}

    narrative = None
    if await _under_daily_cap(db, store_id):
        narrative = await _call_llm(payload, question)
        await _increment_counter(db, store_id)

    entry = AiInsightCache(
        cache_key=key,
        store_id=store_id,
        payload=payload,
        narrative=narrative,
        expires_at=now + timedelta(minutes=CACHE_TTL_MINUTES),
    )
    await db.merge(entry)
    await db.commit()
    return {"facts": payload, "narrative": narrative, "generated_at": now, "from_cache": False}
