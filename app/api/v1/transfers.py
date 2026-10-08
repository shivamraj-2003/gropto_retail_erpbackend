import uuid
from datetime import date, datetime, time, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission, require_store_access, require_warehouse_access
from app.core.database import get_db
from app.models.models import InventoryBalance, Product, ProductBarcode, Store, User
from app.models.models_phase2 import Transfer, Warehouse, WarehouseBalance
from app.schemas.schemas import Page
from app.schemas.schemas_phase2 import (
    TransferCreate,
    TransferDetailOut,
    TransferDiscrepancyResolveIn,
    TransferItemOut,
    TransferOut,
    TransferReceive,
    TransferSourceStockOut,
)
from app.services.transfers import cancel_transfer, dispatch_transfer, receive_transfer, resolve_discrepancy

router = APIRouter(prefix="/transfers", tags=["transfers"])


def _scope_place(kind: str, place_id: uuid.UUID, current: CurrentUser) -> None:
    if kind == "store":
        require_store_access(place_id, current)
    else:
        require_warehouse_access(place_id, current)


def _may_see(transfer: Transfer, current: CurrentUser) -> bool:
    """A store-scoped user sees transfers that touch one of their stores; warehouse legs follow warehouse scope."""
    if current.sees_all_stores():
        return True
    for kind, place_id in ((transfer.source_type, transfer.source_id), (transfer.dest_type, transfer.dest_id)):
        if kind == "store" and current.owns_store(place_id):
            return True
        if kind == "warehouse" and current.owns_warehouse(place_id):
            return True
    return False


async def _build(db: AsyncSession, transfers: list[Transfer], *, with_lines: bool) -> list[dict]:
    """Rows with names (never raw IDs), totals at cost, and optionally every product line."""
    store_ids = {t.source_id for t in transfers if t.source_type == "store"} | {t.dest_id for t in transfers if t.dest_type == "store"}
    wh_ids = {t.source_id for t in transfers if t.source_type == "warehouse"} | {t.dest_id for t in transfers if t.dest_type == "warehouse"}
    stores = {s.id: s.name for s in (await db.execute(select(Store).where(Store.id.in_(store_ids)))).scalars().all()} if store_ids else {}
    whs = {w.id: w.name for w in (await db.execute(select(Warehouse).where(Warehouse.id.in_(wh_ids)))).scalars().all()} if wh_ids else {}
    user_ids = {t.dispatched_by for t in transfers if t.dispatched_by} | {t.received_by for t in transfers if t.received_by}
    users = {u.id: u.full_name for u in (await db.execute(select(User).where(User.id.in_(user_ids)))).scalars().all()} if user_ids else {}
    product_ids = {i.product_id for t in transfers for i in t.items}
    products = {p.id: p for p in (await db.execute(select(Product).where(Product.id.in_(product_ids)))).scalars().all()} if product_ids else {}

    def name_of(kind: str, place_id: uuid.UUID) -> str:
        return (stores if kind == "store" else whs).get(place_id, "(removed)")

    out: list[dict] = []
    for t in transfers:
        lines = []
        units = value = 0.0
        for item in t.items:
            p = products.get(item.product_id)
            cost = float(p.purchase_price) if p else 0.0
            sent = float(item.dispatched_qty)
            got = float(item.received_qty) if item.received_qty is not None else None
            units += sent
            value += sent * cost
            lines.append(
                {
                    "id": item.id,
                    "product_id": item.product_id,
                    "sku": p.sku if p else "?",
                    "name": p.name if p else "(removed product)",
                    "brand": p.brand if p else None,
                    "barcode": p.barcode if p else None,
                    "hsn_code": p.hsn_code if p else None,
                    "tax_rate": float(p.tax_rate) if p else 0.0,
                    "uom": p.uom if p else "EA",
                    "unit_cost": cost,
                    "dispatched_qty": sent,
                    "received_qty": got,
                    "difference": None if got is None else round(got - sent, 3),
                    "value": round(sent * cost, 2),
                }
            )
        row = {
            "id": t.id,
            "transfer_no": t.transfer_no,
            "number": f"TR-{t.transfer_no:06d}",
            "source_type": t.source_type,
            "source_id": t.source_id,
            "source_name": name_of(t.source_type, t.source_id),
            "dest_type": t.dest_type,
            "dest_id": t.dest_id,
            "dest_name": name_of(t.dest_type, t.dest_id),
            "status": t.status,
            "note": t.note,
            "dispatched_at": t.dispatched_at,
            "received_at": t.received_at,
            "dispatched_by_name": users.get(t.dispatched_by),
            "received_by_name": users.get(t.received_by),
            "line_count": len(lines),
            "total_units": round(units, 3),
            "total_value": round(value, 2),
        }
        if with_lines:
            row["items"] = lines
        out.append(row)
    return out


@router.get("", response_model=Page[TransferOut])
async def list_transfers(
    status_filter: str | None = None,
    q: str | None = None,
    place_type: str | None = None,
    place_id: uuid.UUID | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int = 20,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("transfer.transfer.view")),
) -> Page[TransferOut]:
    """`q` finds a transfer by its number (TR-12, 12), a product name/SKU/barcode on it, or a note.
    `place_type`+`place_id` keeps transfers going to or from that place."""
    stmt = select(Transfer)
    if not current.sees_all_stores():
        mine = [s for s in current.store_ids]
        stmt = stmt.where(
            or_(
                and_(Transfer.source_type == "store", Transfer.source_id.in_(mine)),
                and_(Transfer.dest_type == "store", Transfer.dest_id.in_(mine)),
                Transfer.source_type == "warehouse",
                Transfer.dest_type == "warehouse",
            )
        )
    if status_filter:
        stmt = stmt.where(Transfer.status == status_filter)
    if place_type and place_id:
        stmt = stmt.where(
            or_(
                and_(Transfer.source_type == place_type, Transfer.source_id == place_id),
                and_(Transfer.dest_type == place_type, Transfer.dest_id == place_id),
            )
        )
    if date_from:
        stmt = stmt.where(Transfer.dispatched_at >= datetime.combine(date_from, time.min, tzinfo=timezone.utc))
    if date_to:
        stmt = stmt.where(Transfer.dispatched_at <= datetime.combine(date_to, time.max, tzinfo=timezone.utc))
    if q and q.strip():
        term = q.strip()
        digits = term.upper().removeprefix("TR-").lstrip("0")
        like = f"%{term}%"
        from app.models.models_phase2 import TransferItem  # local: avoids widening the import block

        by_product = select(TransferItem.transfer_id).join(Product, Product.id == TransferItem.product_id).where(
            or_(
                Product.name.ilike(like),
                Product.sku.ilike(like),
                Product.barcode.ilike(like),
                Product.id.in_(select(ProductBarcode.product_id).where(ProductBarcode.barcode.ilike(like))),
            )
        )
        conds = [Transfer.id.in_(by_product), Transfer.note.ilike(like)]
        if digits.isdigit():
            conds.append(Transfer.transfer_no == int(digits))
        stmt = stmt.where(or_(*conds))
    capped_limit = min(max(limit, 1), 200)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    result = await db.execute(stmt.order_by(Transfer.dispatched_at.desc()).limit(capped_limit).offset(max(offset, 0)))
    rows = await _build(db, list(result.scalars().all()), with_lines=False)
    return Page(items=rows, total=total, limit=capped_limit, offset=offset)


@router.get("/source-stock", response_model=list[TransferSourceStockOut])
async def source_stock(
    source_type: str,
    source_id: uuid.UUID,
    q: str | None = None,
    limit: int = 30,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("transfer.transfer.create")),
) -> list[dict]:
    """What the sending place actually has (only products with stock), so a transfer can be
    built from real quantities. Searches name, SKU, brand and every barcode of a product."""
    if source_type not in ("store", "warehouse"):
        raise HTTPException(status_code=400, detail="source_type must be store or warehouse")
    _scope_place(source_type, source_id, current)
    if source_type == "store":
        qty_col = InventoryBalance.quantity - InventoryBalance.reserved - InventoryBalance.damaged - InventoryBalance.blocked
        stmt = (
            select(Product, qty_col.label("available"))
            .join(InventoryBalance, InventoryBalance.product_id == Product.id)
            .where(InventoryBalance.store_id == source_id)
        )
    else:
        qty_col = WarehouseBalance.quantity
        stmt = (
            select(Product, qty_col.label("available"))
            .join(WarehouseBalance, WarehouseBalance.product_id == Product.id)
            .where(WarehouseBalance.warehouse_id == source_id)
        )
    stmt = stmt.where(Product.is_active.is_(True), qty_col > 0)
    if q and q.strip():
        like = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                Product.name.ilike(like),
                Product.sku.ilike(like),
                Product.brand.ilike(like),
                Product.barcode.ilike(like),
                Product.id.in_(select(ProductBarcode.product_id).where(ProductBarcode.barcode.ilike(like))),
            )
        )
    rows = (await db.execute(stmt.order_by(Product.name).limit(min(max(limit, 1), 100)))).all()
    return [
        {
            "product_id": p.id,
            "sku": p.sku,
            "name": p.name,
            "brand": p.brand,
            "barcode": p.barcode,
            "hsn_code": p.hsn_code,
            "tax_rate": float(p.tax_rate),
            "uom": p.uom,
            "unit_cost": float(p.purchase_price),
            "available": float(available),
        }
        for p, available in rows
    ]


async def _get_visible(db: AsyncSession, transfer_id: uuid.UUID, current: CurrentUser) -> Transfer:
    transfer = await db.get(Transfer, transfer_id)
    if transfer is None or not _may_see(transfer, current):
        raise HTTPException(status_code=404, detail="Transfer not found")
    return transfer


@router.get("/{transfer_id}", response_model=TransferDetailOut)
async def get_transfer(
    transfer_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("transfer.transfer.view")),
) -> dict:
    transfer = await _get_visible(db, transfer_id, current)
    return (await _build(db, [transfer], with_lines=True))[0]


@router.post("", response_model=TransferOut, status_code=201)
async def create_transfer(
    payload: TransferCreate,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("transfer.transfer.create")),
) -> dict:
    # You may only send from, and to, places you are allowed to use.
    _scope_place(payload.source_type, payload.source_id, current)
    _scope_place(payload.dest_type, payload.dest_id, current)
    transfer = await dispatch_transfer(db, current=current, payload=payload)
    await db.commit()
    await db.refresh(transfer)
    return (await _build(db, [transfer], with_lines=False))[0]


@router.post("/{transfer_id}/receive", response_model=TransferOut)
async def receive(
    transfer_id: uuid.UUID,
    payload: TransferReceive,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("transfer.transfer.update")),
) -> dict:
    transfer = await db.get(Transfer, transfer_id)
    if transfer is None:
        raise HTTPException(status_code=404, detail="Transfer not found")
    _scope_place(transfer.dest_type, transfer.dest_id, current)
    transfer = await receive_transfer(db, current=current, transfer=transfer, payload=payload)
    await db.commit()
    await db.refresh(transfer)
    return (await _build(db, [transfer], with_lines=False))[0]


@router.post("/{transfer_id}/cancel", response_model=TransferOut)
async def cancel(
    transfer_id: uuid.UUID,
    payload: TransferDiscrepancyResolveIn,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("transfer.transfer.update")),
) -> dict:
    """Call off a transfer that is still on its way; the goods go back to the sender."""
    transfer = await db.get(Transfer, transfer_id)
    if transfer is None:
        raise HTTPException(status_code=404, detail="Transfer not found")
    _scope_place(transfer.source_type, transfer.source_id, current)
    transfer = await cancel_transfer(db, current=current, transfer=transfer, reason=payload.note)
    await db.commit()
    await db.refresh(transfer)
    return (await _build(db, [transfer], with_lines=False))[0]


@router.post("/{transfer_id}/resolve-discrepancy")
async def resolve_transfer_discrepancy(
    transfer_id: uuid.UUID,
    payload: TransferDiscrepancyResolveIn,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("transfer.discrepancy.reconcile")),
) -> dict:
    transfer = await db.get(Transfer, transfer_id)
    if transfer is None:
        raise HTTPException(status_code=404, detail="Transfer not found")
    _scope_place(transfer.dest_type, transfer.dest_id, current)
    result = await resolve_discrepancy(db, current=current, transfer=transfer, note=payload.note)
    await db.commit()
    if isinstance(result, dict):
        return {"approval_request_id": str(result["approval_request_id"]), "status": result["status"]}
    return {"status": result.status}
