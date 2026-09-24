"""Finance foundation (Phase 2): expense capture with approval, payables ageing,
store P&L / margin views computed on demand rather than materialized (small enough
data volume at MVP scale)."""

import uuid

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser
from app.models.models_phase2 import Expense, Payable
from app.schemas.schemas_phase2 import ExpenseCreate
from app.services.approvals import submit_or_apply
from app.services.audit import write_audit

EXPENSE_APPROVAL_THRESHOLD = 5000.0


async def create_expense(db: AsyncSession, *, current: CurrentUser, payload: ExpenseCreate) -> Expense:
    expense = Expense(
        store_id=payload.store_id,
        category=payload.category,
        amount=payload.amount,
        description=payload.description,
        created_by=current.user_id,
        status="pending",
    )
    db.add(expense)
    await db.flush()

    if payload.amount > EXPENSE_APPROVAL_THRESHOLD and current.role_code != "super_admin":
        await submit_or_apply(
            db,
            current=current,
            request_type="expense_approval",
            entity_type="expense",
            entity_id=expense.id,
            old_value=None,
            new_value={"amount": payload.amount, "category": payload.category},
            reason=payload.description,
            store_id=payload.store_id,
        )
    else:
        expense.status = "approved"
        await write_audit(
            db,
            user_id=current.user_id,
            role_code=current.role_code,
            store_id=payload.store_id,
            device_id=current.device_id,
            action="expense.approved",
            entity_type="expense",
            entity_id=expense.id,
            new_value={"amount": payload.amount},
        )
    return expense


async def store_pnl(db: AsyncSession, *, store_id: uuid.UUID) -> dict:
    row = (
        await db.execute(
            text(
                """
                select
                    coalesce(sum(s.grand_total), 0) as revenue,
                    coalesce(sum(s.discount_total), 0) as discounts,
                    coalesce((select sum(amount) from expenses where store_id = :store_id and status = 'approved'), 0) as expenses
                from sales s where s.store_id = :store_id and s.status = 'completed'
                  and date_trunc('month', s.billed_at) = date_trunc('month', now())
                """
            ),
            {"store_id": str(store_id)},
        )
    ).one()
    revenue = float(row.revenue)
    expenses = float(row.expenses)
    return {
        "revenue_mtd": revenue,
        "discounts_mtd": float(row.discounts),
        "expenses_mtd": expenses,
        "estimated_contribution_mtd": revenue - expenses,
    }


async def payables_ageing(db: AsyncSession) -> list[dict]:
    rows = (await db.execute(select(Payable).where(Payable.status != "paid"))).scalars().all()
    return [
        {
            "id": str(p.id),
            "vendor_id": str(p.vendor_id),
            "amount_due": float(p.amount_due),
            "due_date": p.due_date.isoformat() if p.due_date else None,
            "status": p.status,
        }
        for p in rows
    ]
