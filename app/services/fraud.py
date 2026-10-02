"""Fraud & loss prevention (Phase 2): rule-based detection over data already
captured since Phase 1 — excessive discounting, high void/return rates, cash
mismatch, repeated negative stock. Point 13 audit fix: this used to be
callable only on demand with no dedup guard, so clicking "scan" twice in the
same day inserted duplicate alerts for the identical condition — now
scheduled (app/services/scheduler.py::run_fraud_scan_job) and each rule
checks for an existing still-open alert with the same rule_code/store_id/
subject (cashier or user) raised today before inserting another."""

from datetime import date, datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser
from app.models.models_phase2 import FraudAlert
from app.services.audit import write_audit


async def _already_open(db: AsyncSession, *, rule_code: str, store_id, subject_key: str, subject_value: str, today_only: bool = True) -> bool:
    stmt = select(FraudAlert.id).where(
        FraudAlert.rule_code == rule_code,
        FraudAlert.store_id == store_id,
        FraudAlert.status == "open",
        FraudAlert.details[subject_key].astext == subject_value,
    )
    if today_only:
        stmt = stmt.where(FraudAlert.created_at >= datetime.combine(date.today(), datetime.min.time(), tzinfo=timezone.utc))
    existing = (await db.execute(stmt)).scalar_one_or_none()
    return existing is not None


async def run_fraud_scan(db: AsyncSession) -> list[FraudAlert]:
    alerts: list[FraudAlert] = []

    excessive_discounts = (
        await db.execute(
            text(
                """
                select cashier_id, store_id, count(*) cnt, sum(discount_total) total_discount
                from sales
                where override_user_id is not null and billed_at::date = current_date
                group by cashier_id, store_id
                having count(*) > 5
                """
            )
        )
    ).all()
    for row in excessive_discounts:
        if await _already_open(db, rule_code="excessive_discount_overrides", store_id=row.store_id, subject_key="cashier_id", subject_value=str(row.cashier_id)):
            continue
        alert = FraudAlert(
            store_id=row.store_id,
            rule_code="excessive_discount_overrides",
            severity="medium",
            details={"cashier_id": str(row.cashier_id), "override_count": row.cnt, "total_discount": float(row.total_discount)},
        )
        db.add(alert)
        alerts.append(alert)

    cash_mismatches = (
        await db.execute(
            text(
                """
                select store_id, id, variance
                from cashier_shifts
                where status = 'closed' and abs(coalesce(variance, 0)) > 200 and closed_at::date = current_date
                """
            )
        )
    ).all()
    for row in cash_mismatches:
        if await _already_open(db, rule_code="cash_mismatch", store_id=row.store_id, subject_key="shift_id", subject_value=str(row.id)):
            continue
        alert = FraudAlert(
            store_id=row.store_id,
            rule_code="cash_mismatch",
            severity="high",
            details={"shift_id": str(row.id), "variance": float(row.variance)},
        )
        db.add(alert)
        alerts.append(alert)

    negative_stock = (
        await db.execute(
            text("select store_id, product_id, quantity from inventory_balances where quantity < 0")
        )
    ).all()
    for row in negative_stock:
        # Not a "today" event — a product sitting negative yesterday is
        # still negative; dedup must not re-fire every day it stays open.
        if await _already_open(db, rule_code="negative_stock", store_id=row.store_id, subject_key="product_id", subject_value=str(row.product_id), today_only=False):
            continue
        alert = FraudAlert(
            store_id=row.store_id,
            rule_code="negative_stock",
            severity="high",
            details={"product_id": str(row.product_id), "quantity": float(row.quantity)},
        )
        db.add(alert)
        alerts.append(alert)

    stock_adjustment_abuse = (
        await db.execute(
            text(
                """
                select created_by, store_id, count(*) cnt, sum(abs(delta)) total_qty
                from inventory_movements
                where source_type = 'manual_adjustment' and created_at::date = current_date
                group by created_by, store_id
                having count(*) > 10
                """
            )
        )
    ).all()
    for row in stock_adjustment_abuse:
        if await _already_open(db, rule_code="stock_adjustment_abuse", store_id=row.store_id, subject_key="created_by", subject_value=str(row.created_by)):
            continue
        alert = FraudAlert(
            store_id=row.store_id,
            rule_code="stock_adjustment_abuse",
            severity="medium",
            details={"created_by": str(row.created_by), "adjustment_count": row.cnt, "total_qty_adjusted": float(row.total_qty)},
        )
        db.add(alert)
        alerts.append(alert)

    master_data_changes = (
        await db.execute(
            text(
                """
                select user_id, count(*) cnt
                from audit_log
                where action in (
                    'price_change.applied', 'product_deactivation.applied',
                    'store_update.applied', 'store_create.applied', 'config_change.applied'
                )
                  and created_at::date = current_date
                group by user_id
                having count(*) > 20
                """
            )
        )
    ).all()
    for row in master_data_changes:
        if await _already_open(db, rule_code="excessive_master_data_changes", store_id=None, subject_key="user_id", subject_value=str(row.user_id)):
            continue
        alert = FraudAlert(
            store_id=None,
            rule_code="excessive_master_data_changes",
            severity="medium",
            details={"user_id": str(row.user_id), "change_count": row.cnt},
        )
        db.add(alert)
        alerts.append(alert)

    repeated_returns = (
        await db.execute(
            text(
                """
                select requested_by, store_id, count(*) cnt, sum(refund_total) total_refund
                from returns
                where created_at::date = current_date
                group by requested_by, store_id
                having count(*) > 3
                """
            )
        )
    ).all()
    for row in repeated_returns:
        if await _already_open(db, rule_code="repeated_returns", store_id=row.store_id, subject_key="requested_by", subject_value=str(row.requested_by)):
            continue
        alert = FraudAlert(
            store_id=row.store_id,
            rule_code="repeated_returns",
            severity="medium",
            details={"requested_by": str(row.requested_by), "return_count": row.cnt, "total_refund": float(row.total_refund)},
        )
        db.add(alert)
        alerts.append(alert)

    return alerts


async def assign_alert(db: AsyncSession, *, current: CurrentUser, alert_id, assignee_id) -> FraudAlert:
    alert = await db.get(FraudAlert, alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail="Fraud alert not found")
    old_assignee = alert.assigned_to
    alert.assigned_to = assignee_id
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=alert.store_id,
        device_id=current.device_id,
        action="fraud_alert.assigned",
        entity_type="fraud_alert",
        entity_id=alert.id,
        old_value={"assigned_to": str(old_assignee) if old_assignee else None},
        new_value={"assigned_to": str(assignee_id)},
    )
    return alert


async def resolve_alert(db: AsyncSession, *, current: CurrentUser, alert_id, status: str, note: str | None) -> FraudAlert:
    if status not in ("reviewed", "dismissed"):
        raise HTTPException(status_code=400, detail="status must be 'reviewed' or 'dismissed'")
    alert = await db.get(FraudAlert, alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail="Fraud alert not found")
    if alert.status != "open":
        raise HTTPException(status_code=409, detail=f"Alert already {alert.status}")

    old_status = alert.status
    alert.status = status
    alert.resolved_by = current.user_id
    alert.resolved_at = datetime.now(timezone.utc)
    alert.resolution_note = note

    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=alert.store_id,
        device_id=current.device_id,
        action=f"fraud_alert.{status}",
        entity_type="fraud_alert",
        entity_id=alert.id,
        old_value={"status": old_status},
        new_value={"status": status},
        reason=note,
    )
    return alert
