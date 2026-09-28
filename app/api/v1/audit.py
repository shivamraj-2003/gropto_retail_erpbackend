"""Read API for the audit trail every other module already writes to
(app/services/audit.py). One engine, one query surface — a TallyPrime-style
edit log: every CREATE/UPDATE/DELETE/APPROVE/REJECT/CANCEL/STATUS change across
every module lands in audit_log via write_audit(), already immutable at the
database grant level (see the initial migration: update/delete revoked for the
application role). This router only ever reads it, filtered and store-scoped."""

import io
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from openpyxl import Workbook
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission
from app.core.database import get_db
from app.models.models import AuditLog
from app.schemas.schemas import AuditEntryOut

router = APIRouter(prefix="/audit", tags=["audit"])


def _scoped_query(current: CurrentUser, *, entity_type: str | None, action: str | None, user_id: uuid.UUID | None,
                   store_id: uuid.UUID | None, date_from: datetime | None, date_to: datetime | None):
    stmt = select(AuditLog)
    # Multi-tenant isolation: a non-Super-Admin only ever sees audit rows for
    # their own stores, never another tenant's — the same store-scoping rule
    # every other list endpoint in this app already applies.
    if current.role_code != "super_admin":
        stmt = stmt.where(AuditLog.store_id.in_(current.store_ids))
    elif store_id is not None:
        stmt = stmt.where(AuditLog.store_id == store_id)
    if entity_type:
        stmt = stmt.where(AuditLog.entity_type == entity_type)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if user_id:
        stmt = stmt.where(AuditLog.user_id == user_id)
    if date_from:
        stmt = stmt.where(AuditLog.created_at >= date_from)
    if date_to:
        stmt = stmt.where(AuditLog.created_at <= date_to)
    return stmt


@router.get("/entries", response_model=list[AuditEntryOut])
async def list_entries(
    entity_type: str | None = None,
    action: str | None = None,
    user_id: uuid.UUID | None = None,
    store_id: uuid.UUID | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    limit: int = 200,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("audit.view")),
) -> list[AuditLog]:
    stmt = _scoped_query(
        current, entity_type=entity_type, action=action, user_id=user_id, store_id=store_id,
        date_from=date_from, date_to=date_to,
    )
    stmt = stmt.order_by(AuditLog.created_at.desc()).limit(min(limit, 1000)).offset(offset)
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.get("/entity/{entity_type}/{entity_id}", response_model=list[AuditEntryOut])
async def entity_history(
    entity_type: str,
    entity_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("audit.view")),
) -> list[AuditLog]:
    """The full, ordered, immutable version history for one record — entry 1
    is its first-ever audited change, each next entry's old_value is (in
    practice) the previous entry's new_value, which is what makes a
    before-vs-after comparison across the whole history meaningful, not just
    for one edit at a time."""
    stmt = select(AuditLog).where(AuditLog.entity_type == entity_type, AuditLog.entity_id == entity_id)
    if current.role_code != "super_admin":
        stmt = stmt.where(AuditLog.store_id.in_(current.store_ids))
    stmt = stmt.order_by(AuditLog.entity_version.asc().nulls_last(), AuditLog.created_at.asc())
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.get("/actions", response_model=list[str])
async def distinct_actions(
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("audit.view")),
) -> list[str]:
    """Populates the action filter dropdown with only the actions that
    actually occur, rather than a hand-maintained list that drifts from the
    ~17 call sites that write them."""
    stmt = select(AuditLog.action).distinct()
    if current.role_code != "super_admin":
        stmt = stmt.where(AuditLog.store_id.in_(current.store_ids))
    result = await db.execute(stmt.order_by(AuditLog.action))
    return list(result.scalars().all())


@router.get("/export")
async def export_entries(
    entity_type: str | None = None,
    action: str | None = None,
    user_id: uuid.UUID | None = None,
    store_id: uuid.UUID | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("audit.view")),
) -> StreamingResponse:
    stmt = _scoped_query(
        current, entity_type=entity_type, action=action, user_id=user_id, store_id=store_id,
        date_from=date_from, date_to=date_to,
    )
    result = await db.execute(stmt.order_by(AuditLog.created_at.desc()).limit(50000))
    rows = result.scalars().all()

    wb = Workbook()
    ws = wb.active
    ws.title = "Audit Trail"
    ws.append([
        "Timestamp", "Action", "Entity Type", "Entity ID", "Version", "User ID", "Role", "Store ID",
        "Device ID", "IP Address", "Reason", "Old Value", "New Value", "Source", "Approval ID",
    ])
    for r in rows:
        ws.append([
            r.created_at.isoformat() if r.created_at else "",
            r.action,
            r.entity_type,
            str(r.entity_id) if r.entity_id else "",
            r.entity_version or "",
            str(r.user_id) if r.user_id else "",
            r.role_code or "",
            str(r.store_id) if r.store_id else "",
            str(r.device_id) if r.device_id else "",
            r.ip_address or "",
            r.reason or "",
            str(r.old_value) if r.old_value else "",
            str(r.new_value) if r.new_value else "",
            r.source,
            str(r.approval_id) if r.approval_id else "",
        ])

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="audit_trail.xlsx"'},
    )
