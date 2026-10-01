import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission, require_store_access
from app.core.database import get_db
from app.models.models_phase2 import Expense, FraudAlert, ReorderPoint
from app.schemas.schemas import Page
from app.schemas.schemas_phase2 import ExpenseCreate, FraudAlertOut, ReorderPointSet
from app.services import finance as finance_service
from app.services import fraud as fraud_service
from app.services.audit import write_audit

router = APIRouter(tags=["phase2-misc"])


@router.put("/inventory/reorder-points")
async def set_reorder_point(
    payload: ReorderPointSet,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("inventory.adjust")),
) -> dict:
    require_store_access(payload.store_id, current)
    stmt = select(ReorderPoint).where(
        ReorderPoint.product_id == payload.product_id, ReorderPoint.store_id == payload.store_id
    )
    existing = (await db.execute(stmt)).scalar_one_or_none()
    if existing:
        existing.min_qty = payload.min_qty
        existing.max_qty = payload.max_qty
        existing.safety_stock = payload.safety_stock
    else:
        rp = ReorderPoint(**payload.model_dump())
        db.add(rp)
    await db.commit()
    return {"status": "ok"}


@router.get("/inventory/replenishment-alerts")
async def replenishment_alerts(
    store_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("inventory.view")),
) -> list[dict]:
    require_store_access(store_id, current)
    rows = (
        await db.execute(
            text(
                """
                select p.id as product_id, p.sku, p.name, coalesce(ib.quantity, 0) as quantity, rp.min_qty, rp.max_qty,
                       (select max(created_at) from audit_log
                        where action = 'inventory.replenishment_marked' and entity_type = 'reorder_point'
                          and entity_id = rp.product_id and store_id = rp.store_id) as last_replenished_at
                from reorder_points rp
                join products p on p.id = rp.product_id
                left join inventory_balances ib on ib.product_id = rp.product_id and ib.store_id = rp.store_id
                where rp.store_id = :store_id and coalesce(ib.quantity, 0) <= rp.min_qty
                order by quantity asc
                """
            ),
            {"store_id": str(store_id)},
        )
    ).all()
    return [
        {
            "product_id": str(r.product_id),
            "sku": r.sku,
            "name": r.name,
            "quantity": float(r.quantity),
            "min_qty": float(r.min_qty),
            "max_qty": float(r.max_qty),
            "last_replenished_at": r.last_replenished_at.isoformat() if r.last_replenished_at else None,
        }
        for r in rows
    ]


@router.post("/inventory/replenishment-alerts/{product_id}/mark-replenished")
async def mark_replenished(
    product_id: uuid.UUID,
    store_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("inventory.adjust")),
) -> dict:
    """Point 4 audit fix: the blueprint's "shelf replenishment" worklist had
    config (reorder points) and a read-only OOS list, but no action a store
    user could take to record "I walked the shelf and restocked this" — this
    is that action, a real audited event a worklist can be keyed off of
    (last_replenished_at above), not a cosmetic button."""
    require_store_access(store_id, current)
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=store_id,
        device_id=current.device_id,
        action="inventory.replenishment_marked",
        entity_type="reorder_point",
        entity_id=product_id,
        new_value={"store_id": str(store_id)},
    )
    await db.commit()
    return {"status": "ok"}


@router.post("/finance/expenses", status_code=201)
async def create_expense(
    payload: ExpenseCreate,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("purchase.manage")),
) -> dict:
    require_store_access(payload.store_id, current)
    expense = await finance_service.create_expense(db, current=current, payload=payload)
    await db.commit()
    return {"expense_id": str(expense.id), "status": expense.status}


@router.get("/finance/expenses")
async def list_expenses(
    store_id: uuid.UUID | None = None,
    limit: int = 50,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("report.export")),
) -> list[dict]:
    stmt = select(Expense).order_by(Expense.created_at.desc()).offset(offset).limit(limit)
    if store_id:
        require_store_access(store_id, current)
        stmt = stmt.where(Expense.store_id == store_id)
    result = await db.execute(stmt)
    expenses = result.scalars().all()
    return [
        {
            "id": str(e.id),
            "store_id": str(e.store_id),
            "category": e.category,
            "amount": float(e.amount),
            "description": e.description,
            "status": e.status,
            "created_at": e.created_at.isoformat() if e.created_at else None,
        }
        for e in expenses
    ]


@router.get("/finance/store-pnl")
async def store_pnl(
    store_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("report.export")),
) -> dict:
    require_store_access(store_id, current)
    return await finance_service.store_pnl(db, store_id=store_id)


@router.get("/finance/payables")
async def payables(
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("report.export")),
) -> list[dict]:
    return await finance_service.payables_ageing(db)


@router.post("/fraud/scan", response_model=list[FraudAlertOut])
async def run_scan(
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("approval.decide")),
) -> list[FraudAlert]:
    alerts = await fraud_service.run_fraud_scan(db)
    await db.commit()
    return alerts


@router.get("/fraud/alerts", response_model=Page[FraudAlertOut])
async def list_alerts(
    status_filter: str = "open",
    limit: int = 20,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("approval.decide")),
) -> Page[FraudAlertOut]:
    stmt = select(FraudAlert).where(FraudAlert.status == status_filter)
    capped_limit = min(limit, 200)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    result = await db.execute(stmt.order_by(FraudAlert.created_at.desc()).limit(capped_limit).offset(offset))
    return Page(items=list(result.scalars().all()), total=total, limit=capped_limit, offset=offset)
