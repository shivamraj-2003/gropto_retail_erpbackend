"""Incentive and penalty workflow (Point 12 audit fix) — previously payroll's
penalties_total/incentives_total were hardcoded to 0.0 with no source of
real adjustments at all. Mirrors services/finance.py::create_expense's
amount-threshold-gated maker-checker: small adjustments apply immediately,
large ones queue through the generic approval engine."""

import uuid

from app.api.deps import CurrentUser
from app.models.models_phase4 import HrAdjustment
from app.schemas.schemas_phase4 import HrAdjustmentCreate
from app.services.approvals import approval_threshold, submit_or_apply
from app.services.audit import write_audit
from sqlalchemy.ext.asyncio import AsyncSession

HR_ADJUSTMENT_APPROVAL_THRESHOLD = 5000.0


async def create_adjustment(db: AsyncSession, *, current: CurrentUser, payload: HrAdjustmentCreate) -> HrAdjustment:
    adjustment = HrAdjustment(
        employee_id=payload.employee_id,
        store_id=payload.store_id,
        adjustment_type=payload.adjustment_type,
        amount=payload.amount,
        month_year=payload.month_year,
        reason=payload.reason,
        requested_by=current.user_id,
        status="pending",
    )
    db.add(adjustment)
    await db.flush()

    if payload.amount > await approval_threshold(db, "hr_adjustment_approval", HR_ADJUSTMENT_APPROVAL_THRESHOLD) and not current.is_super_admin:
        await submit_or_apply(
            db,
            current=current,
            request_type="hr_adjustment_approval",
            entity_type="hr_adjustment",
            entity_id=adjustment.id,
            old_value=None,
            new_value={"amount": payload.amount, "adjustment_type": payload.adjustment_type},
            reason=payload.reason,
            store_id=payload.store_id,
        )
    else:
        adjustment.status = "approved"
        await write_audit(
            db,
            user_id=current.user_id,
            role_code=current.role_code,
            store_id=payload.store_id,
            device_id=current.device_id,
            action="hr_adjustment.approved",
            entity_type="hr_adjustment",
            entity_id=adjustment.id,
            new_value={"amount": payload.amount, "adjustment_type": payload.adjustment_type},
        )
    return adjustment
