"""Stock count / cycle count (Point 4 audit fix). This workflow did not exist
anywhere — /inventory/reconcile only ever compared the cached balance against
the ledger sum (an internal consistency check), never a physical count
entered by a store user against expected quantity.

Maker-checker split across two calls: initiate_count + submit_count_lines is
the "maker" (the person walking the floor counting stock), finalize_count is
the "checker" (someone with inventory.adjust reviewing and applying the
resulting adjustment) — enforced at the API layer by gating finalize behind a
separate permission check, not by this service alone.
"""

from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser
from app.models.models import InventoryBalance, Product
from app.models.models_phase2 import StockCount, StockCountLine
from app.schemas.schemas_phase2 import StockCountCreate, StockCountSubmitIn
from app.services.approvals import submit_or_apply
from app.services.audit import write_audit
from app.services.inventory import apply_movement

HIGH_VARIANCE_THRESHOLD = 50  # absolute units on any single line triggers approval, same bar as a manual adjustment


async def initiate_count(db: AsyncSession, *, current: CurrentUser, payload: StockCountCreate) -> StockCount:
    stmt = select(InventoryBalance.product_id, InventoryBalance.quantity).where(InventoryBalance.store_id == payload.store_id)
    if payload.product_ids:
        stmt = stmt.where(InventoryBalance.product_id.in_(payload.product_ids))
    else:
        stmt = stmt.join(Product, Product.id == InventoryBalance.product_id).where(Product.is_active.is_(True))
    rows = (await db.execute(stmt)).all()
    if not rows:
        raise HTTPException(status_code=400, detail="No stocked products found for this store to count")

    count = StockCount(store_id=payload.store_id, initiated_by=current.user_id, status="counting")
    db.add(count)
    await db.flush()
    for product_id, quantity in rows:
        db.add(StockCountLine(count_id=count.id, product_id=product_id, expected_qty=quantity))
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=payload.store_id,
        device_id=current.device_id,
        action="stock_count.initiated",
        entity_type="stock_count",
        entity_id=count.id,
        new_value={"line_count": len(rows)},
    )
    return count


async def submit_count_lines(db: AsyncSession, *, current: CurrentUser, count: StockCount, payload: StockCountSubmitIn) -> StockCount:
    if count.status != "counting":
        raise HTTPException(status_code=409, detail=f"Count is {count.status}, not open for counting")

    lines_by_product = {line.product_id: line for line in count.lines}
    for submitted in payload.lines:
        line = lines_by_product.get(submitted.product_id)
        if line is None:
            raise HTTPException(status_code=400, detail=f"Product {submitted.product_id} is not part of this count")
        line.counted_qty = submitted.counted_qty
        line.variance = submitted.counted_qty - float(line.expected_qty)

    if all(line.counted_qty is not None for line in count.lines):
        count.status = "pending_approval"

    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=count.store_id,
        device_id=current.device_id,
        action="stock_count.lines_submitted",
        entity_type="stock_count",
        entity_id=count.id,
        new_value={"submitted_lines": len(payload.lines)},
    )
    return count


async def finalize_count(db: AsyncSession, *, current: CurrentUser, count: StockCount) -> StockCount | dict:
    if count.status != "pending_approval":
        raise HTTPException(status_code=409, detail=f"Count is {count.status}, not ready to finalize — every line must be counted first")

    variant_lines = [line for line in count.lines if line.variance is not None and float(line.variance) != 0]
    high_variance = any(abs(float(line.variance)) > HIGH_VARIANCE_THRESHOLD for line in variant_lines)

    if variant_lines and high_variance and current.role_code != "super_admin":
        # Queues instead of applying — a non-Super-Admin finalizing a
        # high-variance count never reaches this function's "applied
        # immediately" path (submit_or_apply only auto-applies for
        # super_admin), so this always comes back pending.
        request = await submit_or_apply(
            db,
            current=current,
            request_type="stock_count_adjustment",
            entity_type="stock_count",
            entity_id=count.id,
            old_value=None,
            new_value={
                "store_id": str(count.store_id),
                "lines": [
                    {"line_id": str(line.id), "product_id": str(line.product_id), "variance": float(line.variance)}
                    for line in variant_lines
                ],
            },
            reason=f"Stock count finalization — {len(variant_lines)} line(s) with variance",
            store_id=count.store_id,
        )
        return {"approval_request_id": request.id, "status": request.status}
    else:
        for line in variant_lines:
            await apply_movement(
                db,
                product_id=line.product_id,
                store_id=count.store_id,
                delta=float(line.variance),
                reason_code="stock_count_adjustment",
                source_type="stock_count_line",
                source_id=line.id,
                created_by=current.user_id,
                device_id=current.device_id,
            )

    count.status = "completed"
    count.finalized_by = current.user_id
    count.completed_at = datetime.now(timezone.utc)

    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=count.store_id,
        device_id=current.device_id,
        action="stock_count.finalized",
        entity_type="stock_count",
        entity_id=count.id,
        new_value={"variant_line_count": len(variant_lines)},
    )
    return count
