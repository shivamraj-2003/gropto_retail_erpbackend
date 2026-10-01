import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission
from app.core.database import get_db
from app.models.models_phase4 import CeoAlert, SuspiciousBillingLog
from app.schemas.schemas_phase4 import CeoAlertOut

router = APIRouter(prefix="/control-tower", tags=["control-tower"])

SALES_DECLINE_THRESHOLD_PCT = 15.0
NEAR_EXPIRY_DAYS = 15
NEAR_EXPIRY_VALUE_THRESHOLD = 10000.0
CASH_MISMATCH_THRESHOLD = 500.0
MARGIN_EROSION_THRESHOLD_PCT = 5.0
VENDOR_FILL_RATE_THRESHOLD_PCT = 80.0
VENDOR_REJECTION_RATE_THRESHOLD_PCT = 10.0
SYSTEM_FAILURE_THRESHOLD = 5


async def _evaluate_ceo_alerts(db: AsyncSession) -> None:
    """Computes real CEO alerts from live data and persists any newly-true
    condition as an active CeoAlert row. Re-run on every /alerts call (cheap
    aggregate queries); a condition already alerted and still active is not
    re-inserted — see the "already active" guard per alert_type/store_id."""

    async def _already_active(alert_type: str, store_id) -> bool:
        existing = await db.execute(
            select(CeoAlert.id).where(
                CeoAlert.alert_type == alert_type,
                CeoAlert.store_id == store_id,
                CeoAlert.status == "active",
            )
        )
        return existing.scalar_one_or_none() is not None

    sales_decline_rows = (
        await db.execute(
            text(
                """
                select store_id,
                       coalesce(sum(grand_total) filter (where billed_at >= current_date - interval '7 days'), 0) as recent,
                       coalesce(sum(grand_total) filter (
                           where billed_at >= current_date - interval '14 days'
                             and billed_at < current_date - interval '7 days'
                       ), 0) as prior
                from sales
                where billed_at >= current_date - interval '14 days'
                group by store_id
                having coalesce(sum(grand_total) filter (
                           where billed_at >= current_date - interval '14 days'
                             and billed_at < current_date - interval '7 days'
                       ), 0) > 0
                """
            )
        )
    ).all()
    for row in sales_decline_rows:
        decline_pct = (float(row.prior) - float(row.recent)) / float(row.prior) * 100
        if decline_pct >= SALES_DECLINE_THRESHOLD_PCT and not await _already_active("sales_decline", row.store_id):
            db.add(
                CeoAlert(
                    alert_type="sales_decline",
                    severity="high" if decline_pct >= 30 else "medium",
                    store_id=row.store_id,
                    title="Sales Decline Flag",
                    description=f"Store revenue dropped {decline_pct:.1f}% vs the previous 7-day period "
                    f"(₹{row.recent:,.2f} vs ₹{row.prior:,.2f}).",
                    action_required="Review promotional activity, stock availability and footfall for this store.",
                )
            )

    near_expiry_rows = (
        await db.execute(
            text(
                """
                select store_id, sum(quantity * purchase_cost) as value_at_risk
                from inventory_batches
                where expiry_date is not null
                  and expiry_date between current_date and current_date + make_interval(days => :days)
                  and quantity > 0
                  and store_id is not null
                group by store_id
                having sum(quantity * purchase_cost) >= :threshold
                """
            ),
            {"days": NEAR_EXPIRY_DAYS, "threshold": NEAR_EXPIRY_VALUE_THRESHOLD},
        )
    ).all()
    for row in near_expiry_rows:
        if not await _already_active("near_expiry_exposure", row.store_id):
            db.add(
                CeoAlert(
                    alert_type="near_expiry_exposure",
                    severity="critical",
                    store_id=row.store_id,
                    title="Near-Expiry Value-at-Risk Threshold Breached",
                    description=f"₹{row.value_at_risk:,.2f} of inventory expiring within {NEAR_EXPIRY_DAYS} days.",
                    action_required="Trigger a mark-down promotion or initiate an inter-store transfer to a high-velocity store.",
                )
            )

    cash_mismatch_rows = (
        await db.execute(
            text(
                """
                select store_id, variance
                from day_close
                where business_date = current_date - interval '1 day'
                  and abs(variance) >= :threshold
                """
            ),
            {"threshold": CASH_MISMATCH_THRESHOLD},
        )
    ).all()
    for row in cash_mismatch_rows:
        if not await _already_active("cash_mismatch", row.store_id):
            db.add(
                CeoAlert(
                    alert_type="cash_mismatch",
                    severity="medium",
                    store_id=row.store_id,
                    title="Cash Deposit Variance Detected",
                    description=f"Yesterday's day-close cash variance was ₹{row.variance:,.2f}.",
                    action_required="Require a cashier shift audit log review and store manager sign-off.",
                )
            )

    margin_rows = (
        await db.execute(
            text(
                """
                select s.store_id,
                       sum(s.grand_total) as revenue,
                       coalesce((select sum(amount) from expenses e where e.store_id = s.store_id and e.status = 'approved'
                           and date_trunc('month', e.created_at) = date_trunc('month', now())), 0) as expenses
                from sales s
                where s.status = 'completed' and date_trunc('month', s.billed_at) = date_trunc('month', now())
                group by s.store_id
                having sum(s.grand_total) > 0
                """
            )
        )
    ).all()
    for row in margin_rows:
        margin_pct = (float(row.revenue) - float(row.expenses)) / float(row.revenue) * 100
        if margin_pct < MARGIN_EROSION_THRESHOLD_PCT and not await _already_active("margin_erosion", row.store_id):
            db.add(
                CeoAlert(
                    alert_type="margin_erosion",
                    severity="high",
                    store_id=row.store_id,
                    title="Margin Erosion Flag",
                    description=f"Store's MTD gross margin is {margin_pct:.1f}%, below the {MARGIN_EROSION_THRESHOLD_PCT:.0f}% threshold.",
                    action_required="Review store expenses and discount patterns for this store.",
                )
            )

    stockout_risk_rows = (
        await db.execute(
            text(
                """
                select rp.store_id, p.name as product_name, coalesce(ib.quantity, 0) as quantity,
                       sum(si.quantity) as units_sold_7d
                from reorder_points rp
                join products p on p.id = rp.product_id
                left join inventory_balances ib on ib.product_id = rp.product_id and ib.store_id = rp.store_id
                join sale_items si on si.product_id = rp.product_id
                join sales s on s.id = si.sale_id and s.store_id = rp.store_id
                where s.billed_at >= current_date - interval '7 days' and s.status = 'completed'
                group by rp.store_id, p.name, ib.quantity
                having coalesce(ib.quantity, 0) > 0 and coalesce(ib.quantity, 0) <= sum(si.quantity) / 7.0 * 3
                """
            )
        )
    ).all()
    for row in stockout_risk_rows:
        if not await _already_active("stockout_risk", row.store_id):
            db.add(
                CeoAlert(
                    alert_type="stockout_risk",
                    severity="high",
                    store_id=row.store_id,
                    title="Top-Selling SKU Likely to Go Out of Stock",
                    description=f"{row.product_name} has {row.quantity:.0f} units left, roughly 3 days of cover at recent sales velocity.",
                    action_required="Raise an urgent reorder or inter-store transfer for this SKU.",
                )
            )

    transfer_overdue_rows = (
        await db.execute(
            text(
                """
                select id, dest_type, dest_id, dispatched_at
                from transfers
                where status = 'dispatched' and dispatched_at < now() - interval '3 days'
                """
            )
        )
    ).all()
    for row in transfer_overdue_rows:
        store_id = row.dest_id if row.dest_type == "store" else None
        if not await _already_active("transfer_overdue", store_id):
            db.add(
                CeoAlert(
                    alert_type="transfer_overdue",
                    severity="medium",
                    store_id=store_id,
                    title="Stock Transfer Overdue",
                    description=f"Transfer {row.id} has been in transit since {row.dispatched_at:%Y-%m-%d}, over 3 days without receipt confirmation.",
                    action_required="Follow up with the receiving location and confirm receipt or investigate loss.",
                )
            )

    vendor_deterioration_rows = (
        await db.execute(
            text(
                """
                select v.id as vendor_id, v.name as vendor_name, vps.fill_rate, vps.rejection_rate
                from vendor_performance_snapshots vps
                join vendors v on v.id = vps.vendor_id
                where vps.snapshot_date = (select max(snapshot_date) from vendor_performance_snapshots vps2 where vps2.vendor_id = vps.vendor_id)
                  and (vps.fill_rate < :fill_threshold or vps.rejection_rate > :rejection_threshold)
                """
            ),
            {"fill_threshold": VENDOR_FILL_RATE_THRESHOLD_PCT, "rejection_threshold": VENDOR_REJECTION_RATE_THRESHOLD_PCT},
        )
    ).all()
    for row in vendor_deterioration_rows:
        if not await _already_active("vendor_deterioration", None):
            db.add(
                CeoAlert(
                    alert_type="vendor_deterioration",
                    severity="medium",
                    store_id=None,
                    title=f"Vendor Performance Deterioration — {row.vendor_name}",
                    description=f"Fill rate {row.fill_rate:.1f}%, rejection rate {row.rejection_rate:.1f}%.",
                    action_required="Review this vendor's recent orders and consider an alternate source.",
                )
            )

    online_sla_rows = (
        await db.execute(
            text(
                """
                select id, allocated_store_id, status, created_at
                from orders
                where status not in ('delivered', 'cancelled', 'refunded')
                  and created_at < now() - interval '24 hours'
                """
            )
        )
    ).all()
    for row in online_sla_rows:
        if not await _already_active("online_sla_breach", row.allocated_store_id):
            db.add(
                CeoAlert(
                    alert_type="online_sla_breach",
                    severity="high",
                    store_id=row.allocated_store_id,
                    title="Online Order SLA Breach",
                    description=f"Order {row.id} is still '{row.status}' over 24 hours after being placed.",
                    action_required="Escalate to online ops for fulfilment or cancellation with customer notification.",
                )
            )

    system_failure_count = (
        await db.execute(
            text("select count(*) from sync_failures where resolved = false and created_at >= current_date - interval '1 day'")
        )
    ).scalar_one()
    if system_failure_count >= SYSTEM_FAILURE_THRESHOLD and not await _already_active("system_failure", None):
        db.add(
            CeoAlert(
                alert_type="system_failure",
                severity="critical",
                store_id=None,
                title="Elevated Sync/Device Failures",
                description=f"{system_failure_count} unresolved sync failures logged in the last 24 hours.",
                action_required="Check Sync Health and device connectivity across stores.",
            )
        )

    await db.commit()


@router.get("/alerts", response_model=list[CeoAlertOut])
async def list_ceo_alerts(
    store_id: uuid.UUID | None = None,
    status_filter: str = "active",
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("approval.decide")),
) -> list[CeoAlert]:
    if status_filter == "active":
        await _evaluate_ceo_alerts(db)

    stmt = select(CeoAlert).where(CeoAlert.status == status_filter).order_by(CeoAlert.created_at.desc())
    if store_id:
        stmt = stmt.where(CeoAlert.store_id == store_id)
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.post("/scan-suspicious", response_model=dict)
async def scan_suspicious_billing(
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("approval.decide")),
) -> dict:
    """Flags cashiers with an unusually high same-day return count — a real
    repeated-refund pattern, not a fabricated placeholder entry."""
    rows = (
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
    flagged = 0
    for row in rows:
        db.add(
            SuspiciousBillingLog(
                store_id=row.store_id,
                cashier_id=row.requested_by,
                risk_score=min(60 + row.cnt * 5, 99),
                pattern_type="repeated_returns",
                details_json={"return_count": row.cnt, "total_refund_amount": float(row.total_refund)},
            )
        )
        flagged += 1
    await db.commit()
    return {"status": "scan_complete", "flagged_patterns": flagged}
