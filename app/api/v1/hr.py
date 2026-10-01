import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission, require_store_access
from app.core.database import get_db
from app.models.models import User
from app.models.models_phase3 import Attendance, Employee, Shift
from app.schemas.schemas import Page
from app.schemas.schemas_phase3 import (
    AttendanceMark,
    AttendanceOut,
    EmployeeCreate,
    EmployeeOut,
    EmployeeUpdate,
    ShiftCreate,
    ShiftOut,
)
from app.services.audit import write_audit

router = APIRouter(prefix="/hr", tags=["hr"])


async def _assert_user_not_already_linked(db: AsyncSession, *, user_id: uuid.UUID, exclude_employee_id: uuid.UUID | None = None) -> None:
    """Point 12 audit fix: user_id was a freeform FK with zero application-
    level validation — nothing stopped the same login account being linked
    to two different employee records, or linking to a user that doesn't
    exist (only the DB-level FK caught that, with an ugly 500)."""
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=400, detail="Linked user account not found")
    stmt = select(Employee.id).where(Employee.user_id == user_id)
    if exclude_employee_id is not None:
        stmt = stmt.where(Employee.id != exclude_employee_id)
    existing = (await db.execute(stmt)).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail="This user account is already linked to another employee")


@router.post("/employees", status_code=201)
async def create_employee(
    payload: EmployeeCreate,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("hr.manage")),
) -> dict:
    require_store_access(payload.store_id, current)
    if payload.user_id is not None:
        await _assert_user_not_already_linked(db, user_id=payload.user_id)
    employee = Employee(**payload.model_dump())
    db.add(employee)
    await db.flush()
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=payload.store_id,
        device_id=current.device_id,
        action="employee.created",
        entity_type="employee",
        entity_id=employee.id,
        new_value=payload.model_dump(mode="json"),
    )
    await db.commit()
    return {"employee_id": str(employee.id)}


@router.get("/employees", response_model=list[EmployeeOut])
async def list_employees(
    store_id: uuid.UUID,
    include_inactive: bool = False,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("hr.manage")),
) -> list[Employee]:
    require_store_access(store_id, current)
    stmt = select(Employee).where(Employee.store_id == store_id)
    if not include_inactive:
        stmt = stmt.where(Employee.is_active.is_(True))
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.patch("/employees/{employee_id}", response_model=EmployeeOut)
async def update_employee(
    employee_id: uuid.UUID,
    payload: EmployeeUpdate,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("hr.manage")),
) -> Employee:
    """Point 12 audit fix: no update or deactivate path existed at all —
    is_active could only ever be set (to True, by default) at creation."""
    employee = await db.get(Employee, employee_id)
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")
    require_store_access(employee.store_id, current)

    old_value = {
        "name": employee.name,
        "designation": employee.designation,
        "department_id": str(employee.department_id) if employee.department_id else None,
        "warehouse_id": str(employee.warehouse_id) if employee.warehouse_id else None,
        "reporting_manager_id": str(employee.reporting_manager_id) if employee.reporting_manager_id else None,
        "user_id": str(employee.user_id) if employee.user_id else None,
        "is_active": employee.is_active,
    }

    updates = payload.model_dump(exclude_unset=True)
    if "user_id" in updates and updates["user_id"] is not None:
        await _assert_user_not_already_linked(db, user_id=updates["user_id"], exclude_employee_id=employee_id)
    if "reporting_manager_id" in updates and updates["reporting_manager_id"] == employee_id:
        raise HTTPException(status_code=400, detail="An employee cannot be their own reporting manager")
    if updates.get("is_active") is False and employee.exited_at is None and "exited_at" not in updates:
        from datetime import date as _date

        updates["exited_at"] = _date.today()

    for field, value in updates.items():
        setattr(employee, field, value)

    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=employee.store_id,
        device_id=current.device_id,
        action="employee.updated",
        entity_type="employee",
        entity_id=employee.id,
        old_value=old_value,
        new_value=updates,
    )
    await db.commit()
    await db.refresh(employee)
    return employee


@router.post("/shifts", status_code=201)
async def create_shift(
    payload: ShiftCreate,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("hr.manage")),
) -> dict:
    require_store_access(payload.store_id, current)
    shift = Shift(**payload.model_dump())
    db.add(shift)
    await db.flush()
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=payload.store_id,
        device_id=current.device_id,
        action="shift.created",
        entity_type="shift",
        entity_id=shift.id,
        new_value=payload.model_dump(mode="json"),
    )
    await db.commit()
    await db.refresh(shift)
    return {"shift_id": str(shift.id)}


@router.get("/shifts", response_model=list[ShiftOut])
async def list_shifts(
    store_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("hr.manage")),
) -> list[Shift]:
    require_store_access(store_id, current)
    result = await db.execute(select(Shift).where(Shift.store_id == store_id))
    return list(result.scalars().all())


@router.put("/attendance")
async def mark_attendance(
    payload: AttendanceMark,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("hr.manage")),
) -> dict:
    # Point 12 audit fix: this endpoint had no store-scoping at all — any
    # hr.manage holder for any store could mark/overwrite attendance for an
    # employee at any other store, and corrections silently overwrote the
    # prior value with no audit trail.
    employee = await db.get(Employee, payload.employee_id)
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")
    require_store_access(employee.store_id, current)

    stmt = select(Attendance).where(
        Attendance.employee_id == payload.employee_id, Attendance.attendance_date == payload.attendance_date
    )
    existing = (await db.execute(stmt)).scalar_one_or_none()
    if existing:
        old_value = {
            "status": existing.status,
            "check_in": existing.check_in.isoformat() if existing.check_in else None,
            "check_out": existing.check_out.isoformat() if existing.check_out else None,
        }
        existing.status = payload.status
        existing.check_in = payload.check_in
        existing.check_out = payload.check_out
        existing.shift_id = payload.shift_id
        action = "attendance.corrected"
    else:
        old_value = None
        db.add(Attendance(**payload.model_dump()))
        action = "attendance.marked"

    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=employee.store_id,
        device_id=current.device_id,
        action=action,
        entity_type="attendance",
        entity_id=payload.employee_id,
        old_value=old_value,
        new_value={"status": payload.status, "attendance_date": payload.attendance_date.isoformat()},
    )
    await db.commit()
    return {"status": "ok"}


@router.get("/attendance", response_model=Page[AttendanceOut])
async def list_attendance(
    employee_id: uuid.UUID,
    limit: int = 20,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("hr.manage")),
) -> Page[AttendanceOut]:
    employee = await db.get(Employee, employee_id)
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")
    require_store_access(employee.store_id, current)

    stmt = select(Attendance).where(Attendance.employee_id == employee_id)
    capped_limit = min(limit, 200)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    result = await db.execute(stmt.order_by(Attendance.attendance_date.desc()).limit(capped_limit).offset(offset))
    rows = list(result.scalars().all())
    return Page(
        items=[AttendanceOut(date=a.attendance_date, status=a.status) for a in rows],
        total=total, limit=capped_limit, offset=offset,
    )
