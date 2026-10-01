import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission, require_store_access
from app.core.database import get_db
from app.models.models_phase4 import BankDeposit, CustomerReceivable, StoreBudget
from app.schemas.schemas_phase4 import (
    BankDepositCreate,
    BankDepositOut,
    ReceivableOut,
    StoreBudgetCreate,
    StoreBudgetOut,
)
from app.services.budget import create_or_update_budget, refresh_actuals

router = APIRouter(prefix="/finance-advanced", tags=["finance-advanced"])


@router.get("/payment-mode-reconciliation")
async def payment_mode_reconciliation(
    store_id: uuid.UUID | None = None,
    business_date: date | None = None,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("report.export")),
) -> list[dict]:
    """Blueprint §10: "Daily sales and payment reconciliation by store and
    payment mode." Cash mode is cross-checked against that day's bank
    deposit; non-cash modes are reported as recorded-vs-settled placeholders
    (there's no gateway settlement feed wired up yet, so settled is left
    null rather than assumed equal to recorded)."""
    if store_id:
        require_store_access(store_id, current)
    params: dict = {}
    filters = ["s.status = 'completed'"]
    if store_id:
        filters.append("s.store_id = :store_id")
        params["store_id"] = str(store_id)
    if business_date:
        filters.append("s.billed_at::date = :business_date")
        params["business_date"] = business_date
    else:
        filters.append("s.billed_at::date = current_date")
    where_clause = " and ".join(filters)

    rows = (
        await db.execute(
            text(
                f"""
                select st.id as store_id, st.name as store_name, p.mode,
                       sum(p.amount) as recorded_amount, count(distinct s.id) as bill_count
                from payments p
                join sales s on s.id = p.sale_id
                join stores st on st.id = s.store_id
                where {where_clause}
                group by st.id, st.name, p.mode
                order by st.name, p.mode
                """
            ),
            params,
        )
    ).all()

    deposit_date_filter = "business_date = :business_date" if business_date else "business_date = current_date"
    cash_deposits = (
        await db.execute(
            text(
                f"select store_id, cash_expected, cash_deposited, variance from bank_deposits "
                f"where {deposit_date_filter}" + (" and store_id = :store_id" if store_id else ""),
            ),
            params,
        )
    ).all()
    deposit_by_store = {str(d.store_id): d for d in cash_deposits}

    result = []
    for r in rows:
        entry = {
            "store_id": str(r.store_id),
            "store_name": r.store_name,
            "mode": r.mode,
            "recorded_amount": float(r.recorded_amount),
            "bill_count": int(r.bill_count),
            "settled_amount": None,
            "variance": None,
        }
        if r.mode == "cash":
            deposit = deposit_by_store.get(str(r.store_id))
            if deposit:
                entry["settled_amount"] = float(deposit.cash_deposited)
                entry["variance"] = float(deposit.variance)
        result.append(entry)
    return result


@router.get("/accounting-export")
async def accounting_export(
    business_date: date,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("report.export")),
) -> dict:
    """Blueprint §10 "Accounting integration/API so ERP remains operational
    source while finance books remain controlled." Exports one business day
    as double-entry journal lines — real sales/expense/payment data, mapped
    to account names rather than to specific chart_of_accounts rows (none are
    seeded yet in a fresh deployment; the accounting system's own import maps
    these names to its ledger, same as any Tally/Zoho/QuickBooks CSV import
    would expect)."""
    sales_by_mode = (
        await db.execute(
            text(
                """
                select p.mode, sum(p.amount) as amount
                from payments p join sales s on s.id = p.sale_id
                where s.billed_at::date = :business_date and s.status = 'completed'
                group by p.mode
                """
            ),
            {"business_date": business_date},
        )
    ).all()
    sale_totals = (
        await db.execute(
            text(
                """
                select coalesce(sum(taxable_value), 0) as taxable, coalesce(sum(cgst_amount), 0) as cgst,
                       coalesce(sum(sgst_amount), 0) as sgst
                from sale_items si join sales s on s.id = si.sale_id
                where s.billed_at::date = :business_date and s.status = 'completed'
                """
            ),
            {"business_date": business_date},
        )
    ).one()
    expenses = (
        await db.execute(
            text(
                "select category, sum(amount) as amount from expenses "
                "where created_at::date = :business_date and status = 'approved' group by category"
            ),
            {"business_date": business_date},
        )
    ).all()

    journal_lines = []
    for row in sales_by_mode:
        journal_lines.append({"account": f"{row.mode.title()} in Hand", "type": "debit", "amount": float(row.amount)})
    if sale_totals.taxable:
        journal_lines.append({"account": "Sales Revenue", "type": "credit", "amount": float(sale_totals.taxable)})
    if sale_totals.cgst:
        journal_lines.append({"account": "CGST Output Payable", "type": "credit", "amount": float(sale_totals.cgst)})
    if sale_totals.sgst:
        journal_lines.append({"account": "SGST Output Payable", "type": "credit", "amount": float(sale_totals.sgst)})
    for row in expenses:
        journal_lines.append({"account": f"Expense - {row.category}", "type": "debit", "amount": float(row.amount)})
        journal_lines.append({"account": "Cash in Hand", "type": "credit", "amount": float(row.amount)})

    total_debits = sum(l["amount"] for l in journal_lines if l["type"] == "debit")
    total_credits = sum(l["amount"] for l in journal_lines if l["type"] == "credit")
    return {
        "business_date": business_date.isoformat(),
        "journal_lines": journal_lines,
        "total_debits": round(total_debits, 2),
        "total_credits": round(total_credits, 2),
        "balanced": abs(total_debits - total_credits) < 0.01,
    }


@router.get("/consolidated-pnl")
async def consolidated_pnl(
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("report.export")),
) -> dict:
    """Blueprint §10: "Store-wise and consolidated P&L." store_pnl() (services/
    finance.py) only ever computed one store at a time — this rolls every
    active store up into a single company-wide figure plus the per-store
    breakdown in the same call."""
    rows = (
        await db.execute(
            text(
                """
                select st.id as store_id, st.name as store_name,
                    coalesce((select sum(s.grand_total) from sales s where s.store_id = st.id and s.status = 'completed'
                        and date_trunc('month', s.billed_at) = date_trunc('month', now())), 0) as revenue_mtd,
                    coalesce((select sum(s.discount_total) from sales s where s.store_id = st.id and s.status = 'completed'
                        and date_trunc('month', s.billed_at) = date_trunc('month', now())), 0) as discounts_mtd,
                    coalesce((select sum(e.amount) from expenses e where e.store_id = st.id and e.status = 'approved'
                        and date_trunc('month', e.created_at) = date_trunc('month', now())), 0) as expenses_mtd
                from stores st where st.is_active = true order by st.name
                """
            )
        )
    ).all()
    stores = []
    total_revenue = total_discounts = total_expenses = 0.0
    for r in rows:
        revenue, discounts, expenses = float(r.revenue_mtd), float(r.discounts_mtd), float(r.expenses_mtd)
        total_revenue += revenue
        total_discounts += discounts
        total_expenses += expenses
        stores.append(
            {
                "store_id": str(r.store_id),
                "store_name": r.store_name,
                "revenue_mtd": revenue,
                "discounts_mtd": discounts,
                "expenses_mtd": expenses,
                "net_contribution_mtd": revenue - discounts - expenses,
            }
        )
    return {
        "stores": stores,
        "company": {
            "revenue_mtd": total_revenue,
            "discounts_mtd": total_discounts,
            "expenses_mtd": total_expenses,
            "net_contribution_mtd": total_revenue - total_discounts - total_expenses,
        },
    }


@router.get("/bank-deposits", response_model=list[BankDepositOut])
async def list_bank_deposits(
    store_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("report.export")),
) -> list[BankDeposit]:
    stmt = select(BankDeposit).order_by(BankDeposit.business_date.desc())
    if store_id:
        stmt = stmt.where(BankDeposit.store_id == store_id)
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.post("/bank-deposits", response_model=BankDepositOut, status_code=201)
async def create_bank_deposit(
    payload: BankDepositCreate,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("purchase.manage")),
) -> BankDeposit:
    require_store_access(payload.store_id, current)
    variance = payload.cash_deposited - payload.cash_expected
    deposit = BankDeposit(**payload.model_dump(), variance=variance, status="reconciled" if variance == 0 else "variance_flagged")
    db.add(deposit)
    await db.commit()
    await db.refresh(deposit)
    return deposit


@router.get("/budgets", response_model=list[StoreBudgetOut])
async def list_store_budgets(
    store_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("report.export")),
) -> list[StoreBudget]:
    stmt = select(StoreBudget)
    if store_id:
        stmt = stmt.where(StoreBudget.store_id == store_id)
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.post("/budgets", response_model=StoreBudgetOut, status_code=201)
async def create_store_budget(
    payload: StoreBudgetCreate,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("purchase.manage")),
) -> StoreBudget:
    """Point 10 audit fix: StoreBudget had a model, a read-only API, and a
    frontend tab, but no endpoint could ever create a row — the feature was
    dead in every real deployment."""
    require_store_access(payload.store_id, current)
    budget = await create_or_update_budget(
        db,
        current=current,
        store_id=payload.store_id,
        financial_year=payload.financial_year,
        month=payload.month,
        capex_budget=payload.capex_budget,
        opex_budget=payload.opex_budget,
    )
    await db.commit()
    await db.refresh(budget)
    return budget


@router.post("/budgets/refresh-actuals")
async def refresh_budget_actuals(
    financial_year: int,
    month: int,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("report.export")),
) -> dict:
    """Point 10 audit fix: actual_opex/actual_capex had no computation path
    at all — recomputes both from real approved Expense records."""
    updated = await refresh_actuals(db, financial_year=financial_year, month=month)
    await db.commit()
    return {"budgets_updated": updated}


@router.get("/receivables", response_model=list[ReceivableOut])
async def list_customer_receivables(
    customer_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("report.export")),
) -> list[CustomerReceivable]:
    stmt = select(CustomerReceivable)
    if customer_id:
        stmt = stmt.where(CustomerReceivable.customer_id == customer_id)
    result = await db.execute(stmt)
    return list(result.scalars().all())
