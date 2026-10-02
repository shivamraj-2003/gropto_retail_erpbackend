import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission, require_store_access
from app.core.database import get_db
from app.models.models_phase2 import Expense, FraudAlert, ReorderPoint
from app.schemas.schemas import Page
from app.schemas.schemas_phase2 import ExpenseCreate, FraudAlertOut, ReorderPointSet
from app.schemas.schemas_phase4 import AlertAssign, AlertResolve
from app.services import finance as finance_service
from app.services import fraud as fraud_service
from app.services import fraud_notify
from app.services.audit import write_audit

router = APIRouter(tags=["phase2-misc"])


@router.put("/inventory/reorder-points")
async def set_reorder_point(
    payload: ReorderPointSet,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("inventory.reorder.configure")),
) -> dict:
    require_store_access(payload.store_id, current)
    stmt = select(ReorderPoint).where(
        ReorderPoint.product_id == payload.product_id, ReorderPoint.store_id == payload.store_id
    )
    existing = (await db.execute(stmt)).scalar_one_or_none()
    # Point 7 audit fix: min/max/safety-stock changes were completely
    # unaudited — no user, timestamp, or before/after values captured.
    old_value = (
        {"min_qty": float(existing.min_qty), "max_qty": float(existing.max_qty), "safety_stock": float(existing.safety_stock)}
        if existing
        else None
    )
    if existing:
        existing.min_qty = payload.min_qty
        existing.max_qty = payload.max_qty
        existing.safety_stock = payload.safety_stock
    else:
        rp = ReorderPoint(**payload.model_dump())
        db.add(rp)
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=payload.store_id,
        device_id=current.device_id,
        action="reorder_point.updated" if existing else "reorder_point.created",
        entity_type="reorder_point",
        entity_id=payload.product_id,
        old_value=old_value,
        new_value={"min_qty": payload.min_qty, "max_qty": payload.max_qty, "safety_stock": payload.safety_stock},
    )
    await db.commit()
    return {"status": "ok"}


@router.get("/inventory/replenishment-alerts")
async def replenishment_alerts(
    store_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("inventory.replenishment.view")),
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
    current: CurrentUser = Depends(require_permission("inventory.replenishment.update")),
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
    current: CurrentUser = Depends(require_permission("finance.expense.create")),
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
    current: CurrentUser = Depends(require_permission("finance.expense.view")),
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
    current: CurrentUser = Depends(require_permission("finance.pnl.view")),
) -> dict:
    require_store_access(store_id, current)
    return await finance_service.store_pnl(db, store_id=store_id)


@router.get("/finance/payables")
async def payables(
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("finance.payable.view")),
) -> list[dict]:
    return await finance_service.payables_ageing(db)


@router.post("/fraud/scan", response_model=list[FraudAlertOut])
async def run_scan(
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("fraud.alert.create")),
) -> list[FraudAlert]:
    alerts = await fraud_service.run_fraud_scan(db)
    await db.commit()
    if alerts:
        await fraud_notify.notify_new_alerts(
            db,
            subject=f"Gropto Fraud Scan: {len(alerts)} new alert(s)",
            lines=[f"[{a.severity.upper()}] {a.rule_code.replace('_', ' ')} — {a.details}" for a in alerts],
        )
    return alerts


@router.get("/fraud/alerts", response_model=Page[FraudAlertOut])
async def list_alerts(
    store_id: uuid.UUID | None = None,
    status_filter: str = "open",
    limit: int = 20,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("fraud.alert.view")),
) -> Page[FraudAlertOut]:
    stmt = select(FraudAlert).where(FraudAlert.status == status_filter)
    if store_id:
        require_store_access(store_id, current)
        stmt = stmt.where(FraudAlert.store_id == store_id)
    elif not current.sees_all_stores():
        # Point 13 audit fix: previously unscoped for any approval.decide
        # holder — now restricted to the caller's own stores (global,
        # store_id-null alerts stay visible to everyone with fraud.view).
        stmt = stmt.where((FraudAlert.store_id.in_(current.store_ids)) | (FraudAlert.store_id.is_(None)))
    capped_limit = min(limit, 200)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    result = await db.execute(stmt.order_by(FraudAlert.created_at.desc()).limit(capped_limit).offset(offset))
    return Page(items=list(result.scalars().all()), total=total, limit=capped_limit, offset=offset)


@router.post("/fraud/alerts/{alert_id}/assign", response_model=FraudAlertOut)
async def assign_fraud_alert(
    alert_id: uuid.UUID,
    payload: AlertAssign,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("fraud.alert.assign")),
) -> FraudAlert:
    alert = await db.get(FraudAlert, alert_id)
    if alert is not None and alert.store_id is not None:
        require_store_access(alert.store_id, current)
    alert = await fraud_service.assign_alert(db, current=current, alert_id=alert_id, assignee_id=payload.assignee_id)
    await db.commit()
    await db.refresh(alert)
    return alert


@router.post("/fraud/alerts/{alert_id}/resolve", response_model=FraudAlertOut)
async def resolve_fraud_alert(
    alert_id: uuid.UUID,
    payload: AlertResolve,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("fraud.alert.close")),
) -> FraudAlert:
    if payload.status is None:
        raise HTTPException(status_code=400, detail="status is required ('reviewed' or 'dismissed')")
    existing = await db.get(FraudAlert, alert_id)
    if existing is not None and existing.store_id is not None:
        require_store_access(existing.store_id, current)
    alert = await fraud_service.resolve_alert(db, current=current, alert_id=alert_id, status=payload.status, note=payload.note)
    await db.commit()
    await db.refresh(alert)
    return alert
