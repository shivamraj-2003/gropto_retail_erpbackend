"""Returns & refunds (Phase 2). A return raised against the original bill, with
line-level disposition. Small/recent returns finalize immediately; large or old
returns route through the approval engine, same pattern as everything else."""

from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser
from app.models.models import Sale
from app.models.models_phase2 import Return, ReturnItem
from app.schemas.schemas_phase2 import ReturnCreate
from app.services.approvals import submit_or_apply
from app.services.audit import write_audit
from app.services.inventory import apply_movement

REFUND_APPROVAL_THRESHOLD = 1000.0
AGE_APPROVAL_THRESHOLD_DAYS = 30


async def create_return(db: AsyncSession, *, current: CurrentUser, payload: ReturnCreate) -> Return:
    sale = await db.get(Sale, payload.sale_id)
    if sale is None:
        raise HTTPException(status_code=404, detail="Original sale not found")

    refund_total = sum(item.refund_amount for item in payload.items)
    ret = Return(
        sale_id=payload.sale_id,
        store_id=payload.store_id,
        requested_by=current.user_id,
        reason=payload.reason,
        refund_total=refund_total,
        status="pending",
    )
    db.add(ret)
    await db.flush()
    for item in payload.items:
        db.add(
            ReturnItem(
                return_id=ret.id,
                sale_item_id=item.sale_item_id,
                product_id=item.product_id,
                quantity=item.quantity,
                disposition=item.disposition,
                refund_amount=item.refund_amount,
            )
        )
    await db.flush()

    from sqlalchemy import select

    sale_age_days = 0
    if sale.billed_at is not None:
        billed_at = sale.billed_at
        if billed_at.tzinfo is None:
            billed_at = billed_at.replace(tzinfo=timezone.utc)
        sale_age_days = (datetime.now(timezone.utc) - billed_at).days

    needs_approval = (
        refund_total > REFUND_APPROVAL_THRESHOLD or sale_age_days > AGE_APPROVAL_THRESHOLD_DAYS
    ) and current.role_code != "super_admin"

    if needs_approval:
        await submit_or_apply(
            db,
            current=current,
            request_type="return_approval",
            entity_type="return",
            entity_id=ret.id,
            old_value=None,
            new_value={"refund_total": refund_total, "sale_id": str(payload.sale_id)},
            reason=payload.reason,
            store_id=payload.store_id,
        )
    else:
        await finalize_return(db, ret)
        await write_audit(
            db,
            user_id=current.user_id,
            role_code=current.role_code,
            store_id=payload.store_id,
            device_id=current.device_id,
            action="return.completed",
            entity_type="return",
            entity_id=ret.id,
            new_value={"refund_total": refund_total},
        )
    return ret


async def finalize_return(db: AsyncSession, ret: Return) -> None:
    """Applies stock disposition and marks the return completed. Called either
    directly (small/recent returns) or by the return_approval handler on approval."""
    from sqlalchemy import select

    result = await db.execute(select(ReturnItem).where(ReturnItem.return_id == ret.id))
    items = result.scalars().all()
    for item in items:
        if item.disposition == "saleable":
            await apply_movement(
                db,
                product_id=item.product_id,
                store_id=ret.store_id,
                delta=item.quantity,
                reason_code="return_to_saleable",
                source_type="return_item",
                source_id=item.id,
                created_by=ret.requested_by,
                device_id=None,
            )
        # damaged / vendor_return: removed from sellable stock permanently — tracked
        # via the return record itself rather than the sellable-quantity ledger.
    ret.status = "completed"
