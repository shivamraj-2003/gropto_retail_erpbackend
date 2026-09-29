import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission
from app.core.database import get_db
from app.models.models import ApprovalRequest
from app.schemas.schemas import ApprovalDecisionRequest, ApprovalOut, Page
from app.services.approvals import decide

router = APIRouter(prefix="/approvals", tags=["approvals"])


@router.get("", response_model=Page[ApprovalOut])
async def list_approvals(
    status_filter: str | None = "pending",
    limit: int = 20,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("approval.decide")),
) -> Page[ApprovalOut]:
    stmt = select(ApprovalRequest)
    if status_filter:
        stmt = stmt.where(ApprovalRequest.status == status_filter)
    if current.role_code != "super_admin":
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
    current: CurrentUser = Depends(require_permission("approval.decide")),
) -> ApprovalRequest:
    request = await decide(db, request_id=request_id, approver=current, approve=payload.approve, note=payload.note)
    await db.commit()
    await db.refresh(request)
    return request
