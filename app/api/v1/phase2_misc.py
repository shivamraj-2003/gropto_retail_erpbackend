import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission, require_store_access
from app.core.database import get_db
from app.models.models_phase2 import FraudAlert, ReorderPoint
from app.schemas.schemas import Page
from app.schemas.schemas_phase2 import ExpenseCreate, FraudAlertOut, ReorderPointSet
from app.services import finance as finance_service
from app.services import fraud as fraud_service

router = APIRouter(tags=["phase2-misc"])


@router.put("/inventory/reorder-points")
async def set_reorder_point(
    payload: ReorderPointSet,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("inventory.adjust")),
) -> dict:
    require_store_access(payload.store_id, current)
    stmt = (
        pg_insert(ReorderPoint)
        .values(**payload.model_dump())
        .on_conflict_do_update(
            index_elements=[ReorderPoint.product_id, ReorderPoint.store_id],
            set_={"min_qty": payload.min_qty, "max_qty": payload.max_qty, "safety_stock": payload.safety_stock},
        )
    )
    await db.execute(stmt)
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
                select p.sku, p.name, ib.quantity, rp.min_qty, rp.max_qty
                from inventory_balances ib
                join products p on p.id = ib.product_id
                join reorder_points rp on rp.product_id = ib.product_id and rp.store_id = ib.store_id
                where ib.store_id = :store_id and ib.quantity <= rp.min_qty
                order by ib.quantity asc
                """
            ),
            {"store_id": str(store_id)},
        )
    ).all()
    return [{"sku": r.sku, "name": r.name, "quantity": float(r.quantity), "min_qty": float(r.min_qty), "max_qty": float(r.max_qty)} for r in rows]


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
