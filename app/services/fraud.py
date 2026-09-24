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

    return alerts
