"""Store budget vs actual (Point 10 audit fix). StoreBudget had a model, a
read-only API, and a frontend tab built against it, but zero write path
anywhere — no endpoint could ever create a budget or populate actual_opex,
so the feature could never produce real output. This is the real write path
plus the actual-vs-budget computation from real Expense data."""

import uuid
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser
from app.models.models_phase2 import Expense
from app.models.models_phase4 import StoreBudget
from app.services.audit import write_audit


async def create_or_update_budget(
    db: AsyncSession,
    *,
    current: CurrentUser,
    store_id: uuid.UUID,
    financial_year: int,
    month: int,
    capex_budget: float,
    opex_budget: float,
) -> StoreBudget:
    existing = (
        await db.execute(
            select(StoreBudget).where(
                StoreBudget.store_id == store_id,
                StoreBudget.financial_year == financial_year,
                StoreBudget.month == month,
            )
        )
    ).scalar_one_or_none()

    if existing is not None:
        old_value = {"capex_budget": float(existing.capex_budget), "opex_budget": float(existing.opex_budget)}
        existing.capex_budget = capex_budget
        existing.opex_budget = opex_budget
        existing.updated_at = datetime.now(timezone.utc)
        await write_audit(
            db,
            user_id=current.user_id,
            role_code=current.role_code,
            store_id=store_id,
            device_id=current.device_id,
            action="store_budget.updated",
            entity_type="store_budget",
            entity_id=existing.id,
            old_value=old_value,
            new_value={"capex_budget": capex_budget, "opex_budget": opex_budget},
        )
        return existing

    budget = StoreBudget(
        store_id=store_id,
        financial_year=financial_year,
        month=month,
        capex_budget=capex_budget,
        opex_budget=opex_budget,
        created_by=current.user_id,
    )
    db.add(budget)
    await db.flush()
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=store_id,
        device_id=current.device_id,
        action="store_budget.created",
        entity_type="store_budget",
        entity_id=budget.id,
        new_value={"capex_budget": capex_budget, "opex_budget": opex_budget},
    )
    return budget


CAPEX_EXPENSE_CATEGORIES = {"capex", "equipment", "renovation", "fixtures"}


async def refresh_actuals(db: AsyncSession, *, financial_year: int, month: int) -> int:
    """Recomputes actual_opex/actual_capex for every budget row in this
    financial-year+month from real, approved Expense records. OPEX is every
    approved expense NOT in a capex-flagged category; CAPEX is the reverse —
    no separate capex transaction source exists in this schema, so category
    is the only real signal available to classify them."""
    # Approximate month window within the financial year for a simple,
    # correct-enough monthly rollup (calendar month, FY-anchored only for
    # which year January..March falls under).
    # Bound with real tz-aware datetimes, not naive dates — Expense.created_at
    # is DateTime(timezone=True), and comparing it against a bare date
    # produced a silently-empty range (the bug this comment replaces).
    calendar_year = financial_year if month >= 4 else financial_year + 1
    month_start = datetime(calendar_year, month, 1, tzinfo=timezone.utc)
    month_end = (
        datetime(calendar_year, month + 1, 1, tzinfo=timezone.utc)
        if month < 12
        else datetime(calendar_year + 1, 1, 1, tzinfo=timezone.utc)
    )

    budgets = (
        await db.execute(
            select(StoreBudget).where(StoreBudget.financial_year == financial_year, StoreBudget.month == month)
        )
    ).scalars().all()

    updated = 0
    for budget in budgets:
        expenses = (
            await db.execute(
                select(Expense).where(
                    Expense.store_id == budget.store_id,
                    Expense.status == "approved",
                    Expense.created_at >= month_start,
                    Expense.created_at < month_end,
                )
            )
        ).scalars().all()
        opex = sum(float(e.amount) for e in expenses if e.category not in CAPEX_EXPENSE_CATEGORIES)
        capex = sum(float(e.amount) for e in expenses if e.category in CAPEX_EXPENSE_CATEGORIES)
        budget.actual_opex = round(opex, 2)
        budget.actual_capex = round(capex, 2)
        budget.updated_at = datetime.now(timezone.utc)
        updated += 1
    return updated
