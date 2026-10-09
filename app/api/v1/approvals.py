import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_any_permission, require_permission
from app.core.database import get_db
from app.models.models import ApprovalRequest, ApprovalStep
from app.schemas.schemas import ApprovalDecisionRequest, ApprovalOut, ApprovalStepOut, Page
from app.services.approvals import decide

router = APIRouter(prefix="/approvals", tags=["approvals"])


@router.get("", response_model=Page[ApprovalOut])
async def list_approvals(
    status_filter: str | None = "pending",
    limit: int = 20,
    offset: int = 0,
    store_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("approval.request.view")),
) -> Page[ApprovalOut]:
    stmt = select(ApprovalRequest)
    if store_id is not None:
        # one store's requests, plus company-wide ones that belong to no store (new store, new user ...)
        if not current.owns_store(store_id):
            raise HTTPException(status_code=403, detail="No access to this store")
        stmt = stmt.where((ApprovalRequest.store_id == store_id) | (ApprovalRequest.store_id.is_(None)))
    if status_filter:
        stmt = stmt.where(ApprovalRequest.status == status_filter)
    if not current.sees_all_stores():
        stmt = stmt.where(ApprovalRequest.store_id.in_(current.store_ids))
    capped_limit = min(limit, 200)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    result = await db.execute(stmt.order_by(ApprovalRequest.created_at.desc()).limit(capped_limit).offset(offset))
    return Page(items=list(result.scalars().all()), total=total, limit=capped_limit, offset=offset)


@router.post("/{request_id}/decide", response_model=ApprovalOut)
async def decide_approval(
    request_id: uuid.UUID,
    payload: ApprovalDecisionRequest,
    db: AsyncSession = Depends(get_db),
    # Approve and reject are separate permissions; decide() checks the one
    # that matches payload.approve, plus the approval matrix (approver role,
    # maker-checker, store scope, multi-level).
    current: CurrentUser = Depends(require_any_permission("approval.request.approve", "approval.request.reject")),
) -> ApprovalRequest:
    request = await decide(db, request_id=request_id, approver=current, approve=payload.approve, note=payload.note)
    await db.commit()
    await db.refresh(request)
    return request


@router.get("/{request_id}/history", response_model=list[ApprovalStepOut])
async def approval_history(
    request_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("approval.history.view")),
) -> list[ApprovalStep]:
    request = await db.get(ApprovalRequest, request_id)
    if request is None:
        raise HTTPException(status_code=404, detail="Approval request not found")
    if request.store_id is not None and not current.owns_store(request.store_id):
        raise HTTPException(status_code=403, detail="Not authorized for this store")
    rows = await db.execute(
        select(ApprovalStep).where(ApprovalStep.request_id == request_id).order_by(ApprovalStep.created_at)
    )
    return list(rows.scalars().all())
