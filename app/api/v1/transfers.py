import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission, require_store_access
from app.core.database import get_db
from app.models.models_phase2 import Transfer
from app.schemas.schemas import Page
from app.schemas.schemas_phase2 import TransferCreate, TransferDetailOut, TransferDiscrepancyResolveIn, TransferOut, TransferReceive
from app.services.transfers import dispatch_transfer, receive_transfer, resolve_discrepancy

router = APIRouter(prefix="/transfers", tags=["transfers"])


@router.get("", response_model=Page[TransferOut])
async def list_transfers(
    status_filter: str | None = None,
    limit: int = 20,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("transfer.transfer.view")),
) -> Page[TransferOut]:
    stmt = select(Transfer)
    if status_filter:
        stmt = stmt.where(Transfer.status == status_filter)
    capped_limit = min(limit, 200)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    result = await db.execute(stmt.order_by(Transfer.dispatched_at.desc()).limit(capped_limit).offset(offset))
    return Page(items=list(result.scalars().all()), total=total, limit=capped_limit, offset=offset)


@router.get("/{transfer_id}", response_model=TransferDetailOut)
async def get_transfer(
    transfer_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("transfer.transfer.view")),
) -> Transfer:
    transfer = await db.get(Transfer, transfer_id)
    if transfer is None:
        raise HTTPException(status_code=404, detail="Transfer not found")
    return transfer


@router.post("", response_model=TransferOut, status_code=201)
async def create_transfer(
    payload: TransferCreate,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("transfer.transfer.create")),
) -> Transfer:
    # Point 5 audit fix: previously only the blanket inventory.adjust
    # permission gated this — any holder could dispatch stock out of a store
    # they have no assignment to. Warehouse legs have no per-user ownership
    # model anywhere in this codebase (consistent with every other WMS
    # endpoint), so only the store-side leg is scoped here.
    if payload.source_type == "store":
        require_store_access(payload.source_id, current)
    if payload.dest_type == "store":
        require_store_access(payload.dest_id, current)
    transfer = await dispatch_transfer(db, current=current, payload=payload)
    await db.commit()
    await db.refresh(transfer)
    return transfer


@router.post("/{transfer_id}/receive", response_model=TransferOut)
async def receive(
    transfer_id: uuid.UUID,
    payload: TransferReceive,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("transfer.transfer.update")),
) -> Transfer:
    transfer = await db.get(Transfer, transfer_id)
    if transfer is None:
        raise HTTPException(status_code=404, detail="Transfer not found")
    if transfer.dest_type == "store":
        require_store_access(transfer.dest_id, current)
    transfer = await receive_transfer(db, current=current, transfer=transfer, payload=payload)
    await db.commit()
    await db.refresh(transfer)
    return transfer


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
    if transfer.dest_type == "store":
        require_store_access(transfer.dest_id, current)
    result = await resolve_discrepancy(db, current=current, transfer=transfer, note=payload.note)
    await db.commit()
    if isinstance(result, dict):
        return {"approval_request_id": str(result["approval_request_id"]), "status": result["status"]}
    return {"status": result.status}
