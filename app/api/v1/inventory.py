import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission, require_store_access
from app.core.database import get_db
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
