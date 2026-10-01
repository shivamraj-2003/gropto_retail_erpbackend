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
from app.models.models_phase2 import WarehouseBalance


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
    """Point 7 audit fix: available stock must not equal total physical stock
    — damaged and blocked units are real but unsellable, same as reserved."""
    result = await db.execute(
        select(
            InventoryBalance.quantity, InventoryBalance.reserved, InventoryBalance.damaged, InventoryBalance.blocked
        ).where(InventoryBalance.product_id == product_id, InventoryBalance.store_id == store_id)
    )
    row = result.one_or_none()
    if row is None:
        return 0.0
    return float(row.quantity) - float(row.reserved) - float(row.damaged) - float(row.blocked)


async def _adjust_balance_field(db: AsyncSession, *, field: str, product_id: uuid.UUID, store_id: uuid.UUID, delta: float) -> None:
    """Point 7 audit fix: in_transit/damaged/blocked existed as real columns
    on InventoryBalance but nothing anywhere ever wrote to them — the one
    endpoint that read them hardcoded 0 instead. Shared upsert helper for all
    three, mirroring adjust_reserved's own pattern."""
    column = getattr(InventoryBalance, field)
    stmt = (
        pg_insert(InventoryBalance)
        .values(product_id=product_id, store_id=store_id, quantity=0, **{field: delta})
        .on_conflict_do_update(
            index_elements=[InventoryBalance.product_id, InventoryBalance.store_id],
            set_={field: column + delta},
        )
    )
    await db.execute(stmt)


async def adjust_in_transit(db: AsyncSession, *, product_id: uuid.UUID, store_id: uuid.UUID, delta: float) -> None:
    """Informational pipeline bucket for a store that's the destination of a
    dispatched-but-not-yet-received Transfer — does not affect
    quantity/available (correctness there already comes from the source
    being decremented at dispatch and the destination only credited at
    receipt), just makes "how much is inbound to this store" queryable."""
    await _adjust_balance_field(db, field="in_transit", product_id=product_id, store_id=store_id, delta=delta)


async def adjust_damaged(db: AsyncSession, *, product_id: uuid.UUID, store_id: uuid.UUID, delta: float) -> None:
    await _adjust_balance_field(db, field="damaged", product_id=product_id, store_id=store_id, delta=delta)


async def adjust_blocked(db: AsyncSession, *, product_id: uuid.UUID, store_id: uuid.UUID, delta: float) -> None:
    await _adjust_balance_field(db, field="blocked", product_id=product_id, store_id=store_id, delta=delta)


async def adjust_warehouse_balance(db: AsyncSession, *, product_id: uuid.UUID, warehouse_id: uuid.UUID, delta: float) -> None:
    """Warehouse-side counterpart to apply_movement's store balance update. A full
    warehouse movement ledger (mirroring inventory_movements' audit trail) is a
    further refinement once put-away/picking is built out — this closes the more
    pressing gap, where a warehouse transfer leg didn't move any balance at all."""
    stmt = (
        pg_insert(WarehouseBalance)
        .values(product_id=product_id, warehouse_id=warehouse_id, quantity=delta)
        .on_conflict_do_update(
            index_elements=[WarehouseBalance.product_id, WarehouseBalance.warehouse_id],
            set_={"quantity": WarehouseBalance.quantity + delta},
        )
    )
    await db.execute(stmt)


async def get_warehouse_balance(db: AsyncSession, *, product_id: uuid.UUID, warehouse_id: uuid.UUID) -> float:
    result = await db.execute(
        select(WarehouseBalance.quantity).where(
            WarehouseBalance.product_id == product_id, WarehouseBalance.warehouse_id == warehouse_id
        )
    )
    value = result.scalar_one_or_none()
    return float(value) if value is not None else 0.0


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
