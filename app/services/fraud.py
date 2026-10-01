"""Fraud & loss prevention (Phase 2): rule-based detection over data already
captured since Phase 1 — excessive discounting, high void/return rates, cash
mismatch, repeated negative stock. Runs on demand here (call from a scheduler,
e.g. APScheduler or an external cron hitting POST /fraud/scan, in production)."""


from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models_phase2 import FraudAlert


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
        alert = FraudAlert(
            store_id=row.store_id,
            rule_code="repeated_returns",
            severity="medium",
            details={"requested_by": str(row.requested_by), "return_count": row.cnt, "total_refund": float(row.total_refund)},
        )
        db.add(alert)
        alerts.append(alert)

    return alerts
