"""Role-based KPI scorecards / store staff productivity (Point 12 audit
fix) — previously there was no cross-link at all between an Employee row
and the sales/delivery activity their linked User account generated, so
"productivity" could not be reported on for anyone."""

import uuid
from datetime import date, datetime, time, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import Role, Sale, User
from app.models.models_phase3 import Attendance, Employee, Order


async def store_productivity(db: AsyncSession, *, store_id: uuid.UUID, month_start: date, month_end: date) -> list[dict]:
    # Sale.billed_at / Order.delivered_at are tz-aware DateTime columns;
    # comparing them directly against naive `date` bounds silently matches
    # zero rows (same class of bug fixed in services/budget.py for Point 10).
    billed_from = datetime.combine(month_start, time.min, tzinfo=timezone.utc)
    billed_to = datetime.combine(month_end, time.max, tzinfo=timezone.utc)
    employees = (
        await db.execute(select(Employee).where(Employee.store_id == store_id, Employee.is_active.is_(True)))
    ).scalars().all()
    if not employees:
        return []

    employee_ids = [e.id for e in employees]
    user_ids = [e.user_id for e in employees if e.user_id is not None]

    roles_by_user: dict[uuid.UUID, str] = {}
    if user_ids:
        rows = (
            await db.execute(
                select(User.id, Role.code).join(Role, Role.id == User.role_id).where(User.id.in_(user_ids))
            )
        ).all()
        roles_by_user = {row[0]: row[1] for row in rows}

    attendance_rows = (
        await db.execute(
            select(
                Attendance.employee_id,
                Attendance.status,
                func.count().label("cnt"),
            )
            .where(
                Attendance.employee_id.in_(employee_ids),
                Attendance.attendance_date >= month_start,
                Attendance.attendance_date <= month_end,
            )
            .group_by(Attendance.employee_id, Attendance.status)
        )
    ).all()
    attendance_by_employee: dict[uuid.UUID, dict[str, int]] = {}
    for employee_id, status, cnt in attendance_rows:
        attendance_by_employee.setdefault(employee_id, {})[status] = cnt

    sales_rows = (
        await db.execute(
            select(
                Sale.cashier_id,
                func.count().label("cnt"),
                func.coalesce(func.sum(Sale.grand_total), 0).label("revenue"),
            )
            .where(
                Sale.cashier_id.in_(user_ids) if user_ids else Sale.cashier_id.is_(None),
                Sale.store_id == store_id,
                Sale.status == "completed",
                Sale.billed_at >= billed_from,
                Sale.billed_at <= billed_to,
            )
            .group_by(Sale.cashier_id)
        )
    ).all() if user_ids else []
    sales_by_user = {row[0]: (row[1], float(row[2])) for row in sales_rows}

    orders_rows = (
        await db.execute(
            select(Order.rider_id, func.count().label("cnt"))
            .where(
                Order.rider_id.in_(user_ids) if user_ids else Order.rider_id.is_(None),
                Order.allocated_store_id == store_id,
                Order.status == "delivered",
                Order.delivered_at >= billed_from,
                Order.delivered_at <= billed_to,
            )
            .group_by(Order.rider_id)
        )
    ).all() if user_ids else []
    orders_by_user = {row[0]: row[1] for row in orders_rows}

    result = []
    for emp in employees:
        attendance = attendance_by_employee.get(emp.id, {})
        sales_cnt, sales_revenue = sales_by_user.get(emp.user_id, (0, 0.0)) if emp.user_id else (0, 0.0)
        orders_delivered = orders_by_user.get(emp.user_id, 0) if emp.user_id else 0
        result.append(
            {
                "employee_id": emp.id,
                "name": emp.name,
                "designation": emp.designation,
                "role_code": roles_by_user.get(emp.user_id) if emp.user_id else None,
                "worked_days": attendance.get("present", 0) + attendance.get("late", 0),
                "late_count": attendance.get("late", 0),
                "absent_count": attendance.get("absent", 0),
                "sales_count": sales_cnt,
                "sales_revenue": sales_revenue,
                "orders_delivered": orders_delivered,
            }
        )
    return result
