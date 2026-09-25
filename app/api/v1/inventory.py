import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission, require_store_access
from app.core.database import get_db
from app.models.models import InventoryBalance, Product
from app.schemas.schemas import InventoryBalanceOut, StockAdjustmentRequest
from app.services.approvals import submit_or_apply
from app.services.audit import write_audit
from app.services.inventory import apply_movement, get_balance

router = APIRouter(prefix="/inventory", tags=["inventory"])

HIGH_ADJUSTMENT_THRESHOLD = 50  # absolute units; above this a stock adjustment needs approval


@router.get("/balance", response_model=InventoryBalanceOut)
async def read_balance(
    product_id: uuid.UUID,
    store_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("inventory.view")),
) -> dict:
    require_store_access(store_id, current)
    quantity = await get_balance(db, product_id=product_id, store_id=store_id)
    return {
        "product_id": product_id,
        "store_id": store_id,
        "quantity": quantity,
        "reserved": 0,
        "in_transit": 0,
        "damaged": 0,
        "blocked": 0,
    }


@router.post("/adjust")
async def adjust_stock(
    payload: StockAdjustmentRequest,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("inventory.adjust")),
) -> dict:
    require_store_access(payload.store_id, current)

    if abs(payload.delta) > HIGH_ADJUSTMENT_THRESHOLD and current.role_code != "super_admin":
        request = await submit_or_apply(
            db,
            current=current,
            request_type="high_stock_adjustment",
            entity_type="inventory_balance",
            entity_id=payload.product_id,
            old_value=None,
            new_value={
                "product_id": str(payload.product_id),
                "store_id": str(payload.store_id),
                "delta": payload.delta,
                "reason_code": payload.reason_code,
            },
            reason=payload.reason,
            store_id=payload.store_id,
        )
        await db.commit()
        return {"approval_request_id": str(request.id), "status": request.status}

    movement = await apply_movement(
        db,
        product_id=payload.product_id,
        store_id=payload.store_id,
        delta=payload.delta,
        reason_code=payload.reason_code,
        source_type="manual_adjustment",
        source_id=uuid.uuid4(),
        created_by=current.user_id,
        device_id=current.device_id,
    )
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=payload.store_id,
        device_id=current.device_id,
        action="inventory.adjusted",
        entity_type="inventory_balance",
        entity_id=payload.product_id,
        new_value={"delta": payload.delta, "reason": payload.reason},
    )
    await db.commit()
    return {"movement_id": str(movement.id), "status": "applied"}


@router.get("/store-snapshot")
async def store_snapshot(
    store_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("inventory.view")),
) -> list[dict]:
    """Bulk stock pull for a device's local stock_snapshot cache — one call instead
    of one round-trip per SKU. Advisory on the device once cached; this endpoint is
    the source of truth at read time."""
    require_store_access(store_id, current)
    stmt = (
        select(Product.id, Product.sku, Product.name, InventoryBalance.quantity)
        .join(
            InventoryBalance,
            (InventoryBalance.product_id == Product.id) & (InventoryBalance.store_id == store_id),
            isouter=True,
        )
        .where(Product.is_active.is_(True))
    )
    rows = (await db.execute(stmt)).all()
    return [
        {"product_id": str(r.id), "sku": r.sku, "name": r.name, "quantity": float(r.quantity or 0)}
        for r in rows
    ]


@router.post("/reconcile")
async def reconcile_balances(
    apply_fix: bool = False,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("device.manage")),
) -> dict:
    """The ledger-recompute job from §5 of the plan: 'a nightly job recomputes
    balances from the ledger and reports any drift'. No cron infra exists yet, so
    this is the same computation exposed as an on-demand Super Admin action —
    point an external scheduler at it later without changing this endpoint.
    With apply_fix=false (default) it only reports drift; with apply_fix=true it
    corrects inventory_balances to match the ledger sum."""
    drift_rows = (
        await db.execute(
            text(
                """
                select ib.product_id, ib.store_id, ib.quantity as cached_quantity,
                       coalesce(sum(im.delta), 0) as ledger_quantity
                from inventory_balances ib
                left join inventory_movements im
                  on im.product_id = ib.product_id and im.store_id = ib.store_id
                group by ib.product_id, ib.store_id, ib.quantity
                having ib.quantity != coalesce(sum(im.delta), 0)
                """
            )
        )
    ).all()

    drift = [
        {
            "product_id": str(r.product_id),
            "store_id": str(r.store_id),
            "cached_quantity": float(r.cached_quantity),
            "ledger_quantity": float(r.ledger_quantity),
            "difference": float(r.cached_quantity) - float(r.ledger_quantity),
        }
        for r in drift_rows
    ]

    if apply_fix and drift:
        for row in drift:
            await db.execute(
                text(
                    "update inventory_balances set quantity = :q where product_id = :p and store_id = :s"
                ),
                {"q": row["ledger_quantity"], "p": row["product_id"], "s": row["store_id"]},
            )
        await write_audit(
            db,
            user_id=current.user_id,
            role_code=current.role_code,
            store_id=None,
            device_id=current.device_id,
            action="inventory.reconciled",
            entity_type="inventory_balance",
            entity_id=None,
            new_value={"drift_count": len(drift), "fixed": True},
        )
        await db.commit()

    return {"drift_count": len(drift), "drift": drift, "applied_fix": apply_fix and bool(drift)}
