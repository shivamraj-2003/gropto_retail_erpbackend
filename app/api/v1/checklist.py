"""Point 18 audit fix: operational/compliance audit checklist — previously
nothing like this existed anywhere (DocumentChecklistItem is HR-only
joining/exit paperwork, audit_log is the immutable change trail — neither
is a store-walk compliance checklist). Completions are auto-seeded 'pending'
the first time a store's checklist is viewed for a date, same pattern as
Point 12's employee document checklist seeding."""

import uuid
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission, require_store_access
from app.core.database import get_db
from app.models.models_phase4 import OperationalChecklistItem, StoreChecklistCompletion
from app.schemas.schemas_phase4 import ChecklistCompleteIn, ChecklistCompletionOut, ChecklistItemOut
from app.services.audit import write_audit

router = APIRouter(prefix="/checklist", tags=["checklist"])


@router.get("/items", response_model=list[ChecklistItemOut])
async def list_checklist_items(
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("store.checklist.view")),
) -> list[OperationalChecklistItem]:
    result = await db.execute(select(OperationalChecklistItem).where(OperationalChecklistItem.is_active.is_(True)))
    return list(result.scalars().all())


@router.get("/completions", response_model=list[ChecklistCompletionOut])
async def list_completions(
    store_id: uuid.UUID,
    business_date: date,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("store.checklist.view")),
) -> list[StoreChecklistCompletion]:
    require_store_access(store_id, current)
    items = (
        await db.execute(select(OperationalChecklistItem).where(OperationalChecklistItem.is_active.is_(True)))
    ).scalars().all()

    if items:
        values = [
            {
                "checklist_item_id": item.id,
                "store_id": store_id,
                "business_date": business_date,
                "status": "pending",
            }
            for item in items
        ]
        stmt = (
            pg_insert(StoreChecklistCompletion)
            .values(values)
            .on_conflict_do_nothing(
                index_elements=["checklist_item_id", "store_id", "business_date"]
            )
        )
        await db.execute(stmt)
        await db.commit()

    existing = (
        await db.execute(
            select(StoreChecklistCompletion).where(
                StoreChecklistCompletion.store_id == store_id,
                StoreChecklistCompletion.business_date == business_date,
            )
        )
    ).scalars().all()
    return list(existing)


@router.post("/completions/{completion_id}/complete", response_model=ChecklistCompletionOut)
async def complete_checklist_item(
    completion_id: uuid.UUID,
    payload: ChecklistCompleteIn,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("store.checklist.update")),
) -> StoreChecklistCompletion:
    completion = await db.get(StoreChecklistCompletion, completion_id)
    if completion is None:
        raise HTTPException(status_code=404, detail="Checklist completion row not found")
    require_store_access(completion.store_id, current)
    if completion.status == "completed":
        raise HTTPException(status_code=409, detail="Already marked completed")

    completion.status = "completed"
    completion.completed_by = current.user_id
    completion.completed_at = datetime.now(timezone.utc)
    completion.notes = payload.notes

    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=completion.store_id,
        device_id=current.device_id,
        action="checklist.completed",
        entity_type="store_checklist_completion",
        entity_id=completion.id,
        new_value={"business_date": completion.business_date.isoformat()},
        reason=payload.notes,
    )
    await db.commit()
    await db.refresh(completion)
    return completion
