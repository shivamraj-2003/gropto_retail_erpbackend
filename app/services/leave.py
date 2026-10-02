"""Leave management (Point 12 audit fix) — previously "leave" existed only
as a cosmetic status value on an attendance row, with zero request/balance/
approval machinery behind it. Real leave types, balances, and a direct
manager-approval workflow (not the generic financial maker-checker — leave
approval is an operational HR decision, not a money control)."""

import uuid
from datetime import date

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser
from app.models.models_phase4 import LeaveBalance, LeaveRequest, LeaveType
from app.services.audit import write_audit


def _inclusive_days(start: date, end: date) -> float:
    return float((end - start).days + 1)


async def get_balance_summary(db: AsyncSession, *, employee_id: uuid.UUID, year: int) -> list[dict]:
    leave_types = (await db.execute(select(LeaveType).where(LeaveType.is_active.is_(True)))).scalars().all()
    balances = {
        b.leave_type_id: b
        for b in (
            await db.execute(select(LeaveBalance).where(LeaveBalance.employee_id == employee_id, LeaveBalance.year == year))
        ).scalars().all()
    }
    requests = (
        await db.execute(
            select(LeaveRequest).where(
                LeaveRequest.employee_id == employee_id,
                LeaveRequest.status.in_(("approved", "pending")),
            )
        )
    ).scalars().all()

    summary = []
    for lt in leave_types:
        balance = balances.get(lt.id)
        allocated = float(balance.allocated_days) if balance else 0.0
        used = sum(float(r.days) for r in requests if r.leave_type_id == lt.id and r.status == "approved" and r.start_date.year == year)
        pending = sum(float(r.days) for r in requests if r.leave_type_id == lt.id and r.status == "pending" and r.start_date.year == year)
        summary.append(
            {
                "leave_type_id": lt.id,
                "leave_type_name": lt.name,
                "year": year,
                "allocated_days": allocated,
                "used_days": used,
                "pending_days": pending,
                "remaining_days": round(allocated - used - pending, 1),
            }
        )
    return summary


async def create_leave_request(db: AsyncSession, *, current: CurrentUser, employee_id: uuid.UUID, leave_type_id: uuid.UUID, start_date: date, end_date: date, reason: str | None) -> LeaveRequest:
    if end_date < start_date:
        raise HTTPException(status_code=400, detail="end_date cannot be before start_date")

    leave_type = await db.get(LeaveType, leave_type_id)
    if leave_type is None or not leave_type.is_active:
        raise HTTPException(status_code=404, detail="Leave type not found")

    days = _inclusive_days(start_date, end_date)

    if leave_type.paid:
        summary = await get_balance_summary(db, employee_id=employee_id, year=start_date.year)
        row = next((s for s in summary if s["leave_type_id"] == leave_type_id), None)
        remaining = row["remaining_days"] if row else 0.0
        if days > remaining:
            raise HTTPException(
                status_code=409,
                detail=f"Insufficient leave balance: {remaining} day(s) remaining for {leave_type.name}, requested {days}",
            )

    request = LeaveRequest(
        employee_id=employee_id,
        leave_type_id=leave_type_id,
        start_date=start_date,
        end_date=end_date,
        days=days,
        reason=reason,
        requested_by=current.user_id,
        status="pending",
    )
    db.add(request)
    await db.flush()
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=None,
        device_id=current.device_id,
        action="leave_request.created",
        entity_type="leave_request",
        entity_id=request.id,
        new_value={"employee_id": str(employee_id), "leave_type_id": str(leave_type_id), "days": days},
        reason=reason,
    )
    return request


async def decide_leave_request(db: AsyncSession, *, current: CurrentUser, request_id: uuid.UUID, approve: bool, note: str | None) -> LeaveRequest:
    from datetime import datetime, timezone

    request = await db.get(LeaveRequest, request_id)
    if request is None:
        raise HTTPException(status_code=404, detail="Leave request not found")
    if request.status != "pending":
        raise HTTPException(status_code=409, detail=f"Leave request already {request.status}")

    request.status = "approved" if approve else "rejected"
    request.decided_by = current.user_id
    request.decided_at = datetime.now(timezone.utc)
    request.decision_note = note

    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=None,
        device_id=current.device_id,
        action=f"leave_request.{request.status}",
        entity_type="leave_request",
        entity_id=request.id,
        new_value={"status": request.status},
        reason=note,
    )
    return request
