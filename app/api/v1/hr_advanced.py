import uuid
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission, require_store_access
from app.core.database import get_db
from app.models.models_phase3 import Attendance, Employee
from app.models.models_phase4 import PayrollSummaryExport, StaffTransfer
from app.schemas.schemas_phase4 import (
    PayrollExportOut,
    StaffTransferCreate,
    StaffTransferOut,
)
from app.services.audit import write_audit

router = APIRouter(prefix="/hr-advanced", tags=["hr-advanced"])


@router.get("/transfers", response_model=list[StaffTransferOut])
async def list_staff_transfers(
    employee_id: uuid.UUID | None = None,
    store_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("hr.manage")),
) -> list[StaffTransfer]:
    # Point 12 audit fix: this previously returned every transfer
    # company-wide with no store filter at all.
    stmt = select(StaffTransfer).order_by(StaffTransfer.transfer_date.desc())
    if employee_id:
        stmt = stmt.where(StaffTransfer.employee_id == employee_id)
    if store_id is not None:
        require_store_access(store_id, current)
        stmt = stmt.where((StaffTransfer.from_store_id == store_id) | (StaffTransfer.to_store_id == store_id))
    elif not current.sees_all_stores():
        stmt = stmt.where(
            StaffTransfer.from_store_id.in_(current.store_ids) | StaffTransfer.to_store_id.in_(current.store_ids)
        )
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.post("/transfers", response_model=StaffTransferOut, status_code=201)
async def create_staff_transfer(
    payload: StaffTransferCreate,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("hr.manage")),
) -> StaffTransfer:
    """Point 12 audit fix: this used to only insert a log row — the
    employee's actual store assignment was never updated, so the transfer
    log and the live employee directory silently diverged. Both
    from_store_id and to_store_id are now store-access-checked (only
    from_store_id was before), and the employee's real record is consistent
    with from_store_id at the time of the call."""
    require_store_access(payload.from_store_id, current)
    require_store_access(payload.to_store_id, current)

    employee = await db.get(Employee, payload.employee_id)
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")
    if employee.store_id != payload.from_store_id:
        raise HTTPException(
            status_code=409,
            detail=f"Employee is currently assigned to a different store than from_store_id ({employee.store_id})",
        )
    if payload.from_store_id == payload.to_store_id:
        raise HTTPException(status_code=400, detail="from_store_id and to_store_id must be different")

    transfer = StaffTransfer(**payload.model_dump(), status="approved")
    db.add(transfer)
    old_store_id = employee.store_id
    employee.store_id = payload.to_store_id
    await db.flush()

    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=payload.to_store_id,
        device_id=current.device_id,
        action="employee.transferred",
        entity_type="employee",
        entity_id=employee.id,
        old_value={"store_id": str(old_store_id)},
        new_value={"store_id": str(payload.to_store_id), "transfer_id": str(transfer.id), "reason": payload.reason},
        reason=payload.reason,
    )
    await db.commit()
    await db.refresh(transfer)
    return transfer


async def _compute_payroll_summary(db: AsyncSession, *, store_id: uuid.UUID, month_year: str) -> dict:
    """Point 12 audit fix: this used to hardcode
    total_employees=12/worked_days=26.0/overtime_hours=48.5/
    penalties_total=450.0/incentives_total=3200.0 for ANY store/month with
    no existing row, then persist the fabrication. This computes the real,
    available numbers from Employee/Attendance. penalties_total and
    incentives_total are correctly 0 — there is no wage-rate/deduction/
    incentive field anywhere in this schema to derive a real currency
    amount from, and inventing a rate would be exactly the kind of mock
    data this fix removes."""
    year, month = (int(p) for p in month_year.split("-"))
    month_start = date(year, month, 1)
    month_end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)

    total_employees = (
        await db.execute(
            select(func.count()).select_from(Employee).where(Employee.store_id == store_id, Employee.is_active.is_(True))
        )
    ).scalar_one()

    rows = (
        await db.execute(
            select(Attendance.status, Attendance.check_in, Attendance.check_out)
            .join(Employee, Employee.id == Attendance.employee_id)
            .where(
                Employee.store_id == store_id,
                Attendance.attendance_date >= month_start,
                Attendance.attendance_date < month_end,
            )
        )
    ).all()

    worked_days = sum(1 for r in rows if r.status in ("present", "late"))
    overtime_hours = 0.0
    for r in rows:
        if r.check_in and r.check_out:
            hours = (r.check_out - r.check_in).total_seconds() / 3600
            overtime_hours += max(hours - 8.0, 0.0)

    return {
        "total_employees": int(total_employees),
        "worked_days": round(float(worked_days), 1),
        "overtime_hours": round(overtime_hours, 1),
        "penalties_total": 0.0,
        "incentives_total": 0.0,
    }


@router.get("/payroll-summary", response_model=list[PayrollExportOut])
async def get_payroll_summary(
    store_id: uuid.UUID,
    month_year: str = "2026-09",
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("hr.manage")),
) -> list[PayrollSummaryExport]:
    require_store_access(store_id, current)
    computed = await _compute_payroll_summary(db, store_id=store_id, month_year=month_year)

    existing = (
        await db.execute(
            select(PayrollSummaryExport).where(
                PayrollSummaryExport.store_id == store_id, PayrollSummaryExport.month_year == month_year
            )
        )
    ).scalar_one_or_none()

    if existing is not None:
        for field, value in computed.items():
            setattr(existing, field, value)
        existing.generated_at = datetime.now(timezone.utc)
        export = existing
    else:
        export = PayrollSummaryExport(store_id=store_id, month_year=month_year, **computed)
        db.add(export)

    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=store_id,
        device_id=current.device_id,
        action="payroll_summary.generated",
        entity_type="payroll_summary_export",
        entity_id=None,
        new_value={"month_year": month_year, **computed},
    )
    await db.commit()
    await db.refresh(export)
    return [export]
