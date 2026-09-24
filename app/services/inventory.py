"""Stock is an append-only ledger of deltas; the current balance is their sum.
Callers never 'set stock to N' — they record 'minus 2 because of this sale'. Deltas
commute, so two devices applying movements in different orders still converge to the
same balance. A unique index on (source_type, source_id, product_id) makes
double-application of the same source line impossible even under sync replay.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import InventoryBalance, InventoryMovement


class DuplicateMovement(Exception):
    """Raised when the same (source_type, source_id, product_id) was already applied."""


async def apply_movement(
    db: AsyncSession,
    *,
    product_id: uuid.UUID,
    store_id: uuid.UUID,
    delta: float,
    reason_code: str,
    source_type: str,
    source_id: uuid.UUID,
    created_by: uuid.UUID | None,
    device_id: uuid.UUID | None,
) -> InventoryMovement:
    movement = InventoryMovement(
        product_id=product_id,
        store_id=store_id,
        delta=delta,
        reason_code=reason_code,
        source_type=source_type,
        source_id=source_id,
        created_by=created_by,
        device_id=device_id,
    )
    db.add(movement)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise DuplicateMovement() from exc

    stmt = (
        pg_insert(InventoryBalance)
        .values(product_id=product_id, store_id=store_id, quantity=delta)
        .on_conflict_do_update(
            index_elements=[InventoryBalance.product_id, InventoryBalance.store_id],
            set_={"quantity": InventoryBalance.quantity + delta, "updated_at": movement.created_at},
        )
    )
    await db.execute(stmt)
    return movement


async def get_balance(db: AsyncSession, *, product_id: uuid.UUID, store_id: uuid.UUID) -> float:
    result = await db.execute(
        select(InventoryBalance.quantity).where(
            InventoryBalance.product_id == product_id, InventoryBalance.store_id == store_id
        )
    )
    value = result.scalar_one_or_none()
    return float(value) if value is not None else 0.0


async def get_available_to_promise(db: AsyncSession, *, product_id: uuid.UUID, store_id: uuid.UUID) -> float:
    result = await db.execute(
        select(InventoryBalance.quantity, InventoryBalance.reserved).where(
            InventoryBalance.product_id == product_id, InventoryBalance.store_id == store_id
        )
    )
    row = result.one_or_none()
    if row is None:
        return 0.0
    return float(row.quantity) - float(row.reserved)


async def adjust_reserved(db: AsyncSession, *, product_id: uuid.UUID, store_id: uuid.UUID, delta: float) -> None:
    """Online order reservation against available-to-promise. Not part of the
    append-only quantity ledger — reservations are a working figure released or
    converted into a real (ledgered) sale movement on dispatch/cancel."""
    stmt = (
        pg_insert(InventoryBalance)
        .values(product_id=product_id, store_id=store_id, quantity=0, reserved=delta)
        .on_conflict_do_update(
            index_elements=[InventoryBalance.product_id, InventoryBalance.store_id],
            set_={"reserved": InventoryBalance.reserved + delta},
        )
    )
    await db.execute(stmt)
