import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission
from app.core.database import get_db
from app.models.models_phase2 import Transfer
from app.schemas.schemas import Page
from app.schemas.schemas_phase2 import TransferCreate, TransferDetailOut, TransferOut, TransferReceive
from app.services.transfers import dispatch_transfer, receive_transfer

router = APIRouter(prefix="/transfers", tags=["transfers"])


@router.get("", response_model=Page[TransferOut])
async def list_transfers(
    status_filter: str | None = None,
    limit: int = 20,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("inventory.adjust")),
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
    _current: CurrentUser = Depends(require_permission("inventory.adjust")),
) -> Transfer:
    transfer = await db.get(Transfer, transfer_id)
    if transfer is None:
        raise HTTPException(status_code=404, detail="Transfer not found")
    return transfer


@router.post("", response_model=TransferOut, status_code=201)
async def create_transfer(
    payload: TransferCreate,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("inventory.adjust")),
) -> Transfer:
    transfer = await dispatch_transfer(db, current=current, payload=payload)
    await db.commit()
    await db.refresh(transfer)
    return transfer


@router.post("/{transfer_id}/receive", response_model=TransferOut)
async def receive(
    transfer_id: uuid.UUID,
    payload: TransferReceive,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("inventory.adjust")),
) -> Transfer:
    transfer = await db.get(Transfer, transfer_id)
    if transfer is None:
        raise HTTPException(status_code=404, detail="Transfer not found")
    transfer = await receive_transfer(db, current=current, transfer=transfer, payload=payload)
    await db.commit()
    await db.refresh(transfer)
    return transfer
