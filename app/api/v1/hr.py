import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission, require_store_access
from app.core.database import get_db
from app.models.models_phase3 import Attendance, Employee, Shift
from app.schemas.schemas import Page
from app.schemas.schemas_phase3 import AttendanceMark, AttendanceOut, EmployeeCreate, EmployeeOut, ShiftCreate, ShiftOut
from app.services.audit import write_audit

router = APIRouter(prefix="/hr", tags=["hr"])


@router.post("/employees", status_code=201)
async def create_employee(
    payload: EmployeeCreate,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("user.manage")),
) -> dict:
    require_store_access(payload.store_id, current)
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
        new_value={"designation": payload.designation},
    )
    await db.commit()
    return {"employee_id": str(employee.id)}


@router.get("/employees", response_model=list[EmployeeOut])
async def list_employees(
    store_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("user.manage")),
) -> list[Employee]:
    require_store_access(store_id, current)
    result = await db.execute(select(Employee).where(Employee.store_id == store_id, Employee.is_active.is_(True)))
    return list(result.scalars().all())


@router.post("/shifts", status_code=201)
async def create_shift(
    payload: ShiftCreate,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("user.manage")),
) -> dict:
    require_store_access(payload.store_id, current)
    shift = Shift(**payload.model_dump())
    db.add(shift)
    await db.commit()
    await db.refresh(shift)
    return {"shift_id": str(shift.id)}


@router.get("/shifts", response_model=list[ShiftOut])
async def list_shifts(
    store_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("user.manage")),
) -> list[Shift]:
    require_store_access(store_id, current)
    result = await db.execute(select(Shift).where(Shift.store_id == store_id))
    return list(result.scalars().all())


@router.put("/attendance")
async def mark_attendance(
    payload: AttendanceMark,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("user.manage")),
) -> dict:
    stmt = (
        pg_insert(Attendance)
        .values(**payload.model_dump())
        .on_conflict_do_update(
            index_elements=[Attendance.employee_id, Attendance.attendance_date],
            set_={"status": payload.status, "check_in": payload.check_in, "check_out": payload.check_out, "shift_id": payload.shift_id},
        )
    )
    await db.execute(stmt)
    await db.commit()
    return {"status": "ok"}


@router.get("/attendance", response_model=Page[AttendanceOut])
async def list_attendance(
    employee_id: uuid.UUID,
    limit: int = 20,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("user.manage")),
) -> Page[AttendanceOut]:
    stmt = select(Attendance).where(Attendance.employee_id == employee_id)
    capped_limit = min(limit, 200)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    result = await db.execute(stmt.order_by(Attendance.attendance_date.desc()).limit(capped_limit).offset(offset))
    rows = list(result.scalars().all())
    return Page(
        items=[AttendanceOut(date=a.attendance_date, status=a.status) for a in rows],
        total=total, limit=capped_limit, offset=offset,
    )
