import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission
from app.core.database import get_db
from app.models.models import ApprovalRequest
from app.schemas.schemas import ApprovalDecisionRequest, ApprovalOut
from app.services.approvals import decide

router = APIRouter(prefix="/approvals", tags=["approvals"])


@router.get("", response_model=list[ApprovalOut])
async def list_approvals(
    status_filter: str | None = "pending",
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("approval.decide")),
) -> list[ApprovalRequest]:
    stmt = select(ApprovalRequest)
    if status_filter:
        stmt = stmt.where(ApprovalRequest.status == status_filter)
    if current.role_code != "super_admin":
        stmt = stmt.where(ApprovalRequest.store_id.in_(current.store_ids))
    result = await db.execute(stmt.order_by(ApprovalRequest.created_at.desc()).limit(200))
    return list(result.scalars().all())


@router.post("/{request_id}/decide", response_model=ApprovalOut)
async def decide_approval(
    request_id: uuid.UUID,
    payload: ApprovalDecisionRequest,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("approval.decide")),
) -> ApprovalRequest:
    request = await decide(db, request_id=request_id, approver=current, approve=payload.approve, note=payload.note)
    await db.commit()
    await db.refresh(request)
    return request
