import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission, require_store_access
from app.core.database import get_db
from app.models.models_phase4 import CeoAlert, SuspiciousBillingLog
from app.schemas.schemas import Page
from app.schemas.schemas_phase4 import (
    AlertAssign,
    AlertResolve,
    CeoAlertOut,
    SuspiciousBillingLogOut,
)
from app.services import fraud_notify
from app.services.audit import write_audit

router = APIRouter(prefix="/control-tower", tags=["control-tower"])

SALES_DECLINE_THRESHOLD_PCT = 15.0
NEAR_EXPIRY_DAYS = 15
NEAR_EXPIRY_VALUE_THRESHOLD = 10000.0
CASH_MISMATCH_THRESHOLD = 500.0
MARGIN_EROSION_THRESHOLD_PCT = 5.0
# Point 17 audit fix: "sudden margin erosion" (a trend, distinct from the
# static floor above) had no detection at all — this is the month-over-month
# drop-in-percentage-points that counts as "sudden".
MARGIN_EROSION_TREND_PP = 5.0
VENDOR_FILL_RATE_THRESHOLD_PCT = 80.0
VENDOR_REJECTION_RATE_THRESHOLD_PCT = 10.0
# Point 17 audit fix: avg_lead_time_days was computed and stored by
# vendor_performance.py but the CEO alert condition never read it — "lead
# time deterioration" is half this alert's own name and was silently dropped.
VENDOR_LEAD_TIME_THRESHOLD_DAYS = 10.0
SYSTEM_FAILURE_THRESHOLD = 5
# Point 17 audit fix: "store sales below target" had no implementation at
# all — only the comparable-period decline existed. Store.target_revenue_monthly
# already exists (Point 3 audit fix) but nothing read it for alerting.
SALES_BELOW_TARGET_PCT = 70.0  # MTD sales below this % of the prorated monthly target
# Point 17 audit fix: online "cancellation" (as opposed to SLA breach) had no
# detection — only stuck-order SLA breach was implemented.
ONLINE_CANCELLATION_RATE_PCT = 20.0


async def _evaluate_ceo_alerts(db: AsyncSession) -> list[CeoAlert]:
    """Computes real CEO alerts from live data and persists any newly-true
    condition as an active CeoAlert row. Re-run on every /alerts call (cheap
    aggregate queries); a condition already alerted and still active is not
    re-inserted — see the "already active" guard per alert_type/store_id.
    Returns the alerts newly created this call so the caller can notify."""

    new_alerts: list[CeoAlert] = []

    async def _already_active(alert_type: str, store_id, source_id=None) -> bool:
        stmt = select(CeoAlert.id).where(
            CeoAlert.alert_type == alert_type,
            CeoAlert.store_id == store_id,
            CeoAlert.status == "active",
        )
        # Point 17 audit fix: per-entity conditions (vendor/transfer) used to
        # dedup on store_id alone, which is NULL for both — only ever one
        # such alert could be active system-wide no matter how many distinct
        # vendors/transfers were actually deteriorating/overdue.
        if source_id is not None:
            stmt = stmt.where(CeoAlert.source_id == source_id)
        existing = await db.execute(stmt)
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
            new_alert = CeoAlert(
                alert_type="sales_decline",
                severity="high" if decline_pct >= 30 else "medium",
                store_id=row.store_id,
                title="Sales Decline Flag",
                description=f"Store revenue dropped {decline_pct:.1f}% vs the previous 7-day period "
                f"(₹{row.recent:,.2f} vs ₹{row.prior:,.2f}).",
                action_required="Review promotional activity, stock availability and footfall for this store.",
            )
            db.add(new_alert)
            new_alerts.append(new_alert)

    # Point 17 audit fix: real "below target" detection using Store.target_revenue_monthly,
    # prorated by how far into the month we are — no target column was ever read before.
    sales_vs_target_rows = (
        await db.execute(
            text(
                """
                select s.id as store_id, s.target_revenue_monthly,
                       coalesce((select sum(grand_total) from sales
                                 where store_id = s.id and status = 'completed'
                                   and date_trunc('month', billed_at) = date_trunc('month', current_date)), 0) as mtd_sales
                from stores s
                where s.target_revenue_monthly is not null and s.target_revenue_monthly > 0 and s.is_active = true
                """
            )
        )
    ).all()
    days_elapsed = datetime.now(timezone.utc).day
    days_in_month = 30  # conservative prorating window, avoids calendar-library dependency
    for row in sales_vs_target_rows:
        prorated_target = float(row.target_revenue_monthly) * min(days_elapsed, days_in_month) / days_in_month
        if prorated_target <= 0:
            continue
        pct_of_target = float(row.mtd_sales) / prorated_target * 100
        if pct_of_target < SALES_BELOW_TARGET_PCT and not await _already_active("sales_below_target", row.store_id):
            new_alert = CeoAlert(
                alert_type="sales_below_target",
                severity="high" if pct_of_target < 50 else "medium",
                store_id=row.store_id,
                title="Store Sales Below Target",
                description=f"MTD sales ₹{row.mtd_sales:,.2f} is {pct_of_target:.0f}% of the prorated target "
                f"(₹{prorated_target:,.2f} of ₹{float(row.target_revenue_monthly):,.2f} monthly target).",
                action_required="Review footfall, stock availability and local promotions for this store.",
            )
            db.add(new_alert)
            new_alerts.append(new_alert)

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
            new_alert = CeoAlert(
                alert_type="near_expiry_exposure",
                severity="critical",
                store_id=row.store_id,
                title="Near-Expiry Value-at-Risk Threshold Breached",
                description=f"₹{row.value_at_risk:,.2f} of inventory expiring within {NEAR_EXPIRY_DAYS} days.",
                action_required="Trigger a mark-down promotion or initiate an inter-store transfer to a high-velocity store.",
            )
            db.add(new_alert)
            new_alerts.append(new_alert)

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
            new_alert = CeoAlert(
                alert_type="cash_mismatch",
                severity="medium",
                store_id=row.store_id,
                title="Cash Deposit Variance Detected",
                description=f"Yesterday's day-close cash variance was ₹{row.variance:,.2f}.",
                action_required="Require a cashier shift audit log review and store manager sign-off.",
            )
            db.add(new_alert)
            new_alerts.append(new_alert)

    # Point 17 audit fix: bank_deposits (cash_expected/cash_deposited/variance/
    # status) existed as a fully-built table that the alert engine never read —
    # "pending deposit" was entirely unmonitored despite the data existing.
    # Flags a store that closed its day yesterday with no deposit recorded at
    # all, or whose recorded deposit itself has a variance flagged.
    pending_deposit_rows = (
        await db.execute(
            text(
                """
                select dc.store_id,
                       bd.id as deposit_id, bd.variance, bd.status as deposit_status
                from day_close dc
                left join bank_deposits bd
                  on bd.store_id = dc.store_id and bd.business_date = dc.business_date
                where dc.business_date = current_date - interval '1 day'
                  and (bd.id is null or bd.status = 'variance_flagged')
                """
            )
        )
    ).all()
    for row in pending_deposit_rows:
        if await _already_active("cash_pending_deposit", row.store_id):
            continue
        if row.deposit_id is None:
            description = "Yesterday's day-close cash was never deposited to the bank — no deposit record exists."
            severity = "high"
        else:
            description = f"Yesterday's bank deposit has a flagged variance of ₹{float(row.variance):,.2f}."
            severity = "medium"
        new_alert = CeoAlert(
            alert_type="cash_pending_deposit",
            severity=severity,
            store_id=row.store_id,
            title="Pending/Variance Bank Deposit",
            description=description,
            action_required="Confirm the store's cash deposit was made and matches the day-close total.",
        )
        db.add(new_alert)
        new_alerts.append(new_alert)

    margin_rows = (
        await db.execute(
            text(
                """
                select s.store_id,
                       sum(s.grand_total) as revenue,
                       coalesce((select sum(amount) from expenses e where e.store_id = s.store_id and e.status = 'approved'
                           and date_trunc('month', e.created_at) = date_trunc('month', now())), 0) as expenses,
                       coalesce((select sum(grand_total) from sales s2 where s2.store_id = s.store_id and s2.status = 'completed'
                           and date_trunc('month', s2.billed_at) = date_trunc('month', now() - interval '1 month')), 0) as prior_revenue,
                       coalesce((select sum(amount) from expenses e2 where e2.store_id = s.store_id and e2.status = 'approved'
                           and date_trunc('month', e2.created_at) = date_trunc('month', now() - interval '1 month')), 0) as prior_expenses
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
        # Point 17 audit fix: "sudden margin erosion" — a month-over-month
        # percentage-point drop — is distinct from the static floor check
        # below and previously had no detection at all.
        prior_margin_pct = (
            (float(row.prior_revenue) - float(row.prior_expenses)) / float(row.prior_revenue) * 100
            if float(row.prior_revenue) > 0
            else None
        )
        erosion_pp = (prior_margin_pct - margin_pct) if prior_margin_pct is not None else 0.0
        below_floor = margin_pct < MARGIN_EROSION_THRESHOLD_PCT
        sudden_erosion = prior_margin_pct is not None and erosion_pp >= MARGIN_EROSION_TREND_PP
        if (below_floor or sudden_erosion) and not await _already_active("margin_erosion", row.store_id):
            if sudden_erosion and not below_floor:
                description = (
                    f"Store's MTD gross margin dropped {erosion_pp:.1f} percentage points "
                    f"vs last month ({margin_pct:.1f}% from {prior_margin_pct:.1f}%)."
                )
            else:
                description = f"Store's MTD gross margin is {margin_pct:.1f}%, below the {MARGIN_EROSION_THRESHOLD_PCT:.0f}% threshold."
            new_alert = CeoAlert(
                alert_type="margin_erosion",
                severity="high",
                store_id=row.store_id,
                title="Margin Erosion Flag",
                description=description,
                action_required="Review store expenses and discount patterns for this store.",
            )
            db.add(new_alert)
            new_alerts.append(new_alert)

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
            new_alert = CeoAlert(
                alert_type="stockout_risk",
                severity="high",
                store_id=row.store_id,
                title="Top-Selling SKU Likely to Go Out of Stock",
                description=f"{row.product_name} has {row.quantity:.0f} units left, roughly 3 days of cover at recent sales velocity.",
                action_required="Raise an urgent reorder or inter-store transfer for this SKU.",
            )
            db.add(new_alert)
            new_alerts.append(new_alert)

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
            new_alert = CeoAlert(
                alert_type="transfer_overdue",
                severity="medium",
                store_id=store_id,
                title="Stock Transfer Overdue",
                description=f"Transfer {row.id} has been in transit since {row.dispatched_at:%Y-%m-%d}, over 3 days without receipt confirmation.",
                action_required="Follow up with the receiving location and confirm receipt or investigate loss.",
            )
            db.add(new_alert)
            new_alerts.append(new_alert)

    # Point 17 audit fix: "receipt discrepancy" (the second half of this
    # alert condition) was never raised as a CeoAlert at all — only a bare
    # unalerted count existed on the Enterprise exceptions tile, with no
    # severity/notification/resolve workflow. transfers.status='discrepancy'
    # is set by services/transfers.py::receive_transfer() on real
    # received-vs-dispatched quantity mismatches.
    transfer_discrepancy_rows = (
        await db.execute(
            text(
                """
                select id, dest_type, dest_id
                from transfers
                where status = 'discrepancy'
                """
            )
        )
    ).all()
    for row in transfer_discrepancy_rows:
        store_id = row.dest_id if row.dest_type == "store" else None
        if not await _already_active("transfer_discrepancy", store_id, source_id=row.id):
            new_alert = CeoAlert(
                alert_type="transfer_discrepancy",
                severity="medium",
                store_id=store_id,
                source_id=row.id,
                title="Stock Transfer Receipt Discrepancy",
                description=f"Transfer {row.id} was received with a quantity mismatch and needs resolution.",
                action_required="Review the transfer's line-level variance and resolve via the Transfers screen.",
            )
            db.add(new_alert)
            new_alerts.append(new_alert)

    vendor_deterioration_rows = (
        await db.execute(
            text(
                """
                select v.id as vendor_id, v.name as vendor_name, vps.fill_rate, vps.rejection_rate, vps.avg_lead_time_days
                from vendor_performance_snapshots vps
                join vendors v on v.id = vps.vendor_id
                where vps.snapshot_date = (select max(snapshot_date) from vendor_performance_snapshots vps2 where vps2.vendor_id = vps.vendor_id)
                  and (vps.fill_rate < :fill_threshold or vps.rejection_rate > :rejection_threshold
                       or vps.avg_lead_time_days > :lead_time_threshold)
                """
            ),
            {
                "fill_threshold": VENDOR_FILL_RATE_THRESHOLD_PCT,
                "rejection_threshold": VENDOR_REJECTION_RATE_THRESHOLD_PCT,
                "lead_time_threshold": VENDOR_LEAD_TIME_THRESHOLD_DAYS,
            },
        )
    ).all()
    for row in vendor_deterioration_rows:
        # Point 17 audit fix: this used to dedup on (alert_type, store_id=None)
        # only — since store_id is always None here, a second deteriorating
        # vendor could never get its own alert while the first was still
        # active. source_id=vendor_id makes dedup per-vendor, as intended.
        if not await _already_active("vendor_deterioration", None, source_id=row.vendor_id):
            reasons = []
            if row.fill_rate < VENDOR_FILL_RATE_THRESHOLD_PCT:
                reasons.append(f"fill rate {row.fill_rate:.1f}%")
            if row.rejection_rate > VENDOR_REJECTION_RATE_THRESHOLD_PCT:
                reasons.append(f"rejection rate {row.rejection_rate:.1f}%")
            if row.avg_lead_time_days is not None and row.avg_lead_time_days > VENDOR_LEAD_TIME_THRESHOLD_DAYS:
                reasons.append(f"avg lead time {row.avg_lead_time_days:.1f} days")
            new_alert = CeoAlert(
                alert_type="vendor_deterioration",
                severity="medium",
                store_id=None,
                source_id=row.vendor_id,
                title=f"Vendor Performance Deterioration — {row.vendor_name}",
                description=f"{row.vendor_name}: " + ", ".join(reasons) + ".",
                action_required="Review this vendor's recent orders and consider an alternate source.",
            )
            db.add(new_alert)
            new_alerts.append(new_alert)

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
            new_alert = CeoAlert(
                alert_type="online_sla_breach",
                severity="high",
                store_id=row.allocated_store_id,
                title="Online Order SLA Breach",
                description=f"Order {row.id} is still '{row.status}' over 24 hours after being placed.",
                action_required="Escalate to online ops for fulfilment or cancellation with customer notification.",
            )
            db.add(new_alert)
            new_alerts.append(new_alert)

    # Point 17 audit fix: "online cancellation" (a rate/spike, the
    # alternative trigger the blueprint names alongside SLA breach) had no
    # detection at all — only stuck-order SLA breach existed. A store with
    # normal fulfilment but a surge of cancellations today raised nothing.
    online_cancellation_rows = (
        await db.execute(
            text(
                """
                select allocated_store_id,
                       count(*) filter (where status = 'cancelled') as cancelled_count,
                       count(*) as total_count
                from orders
                where created_at >= current_date
                group by allocated_store_id
                having count(*) >= 5
                """
            )
        )
    ).all()
    for row in online_cancellation_rows:
        cancellation_pct = float(row.cancelled_count) / float(row.total_count) * 100
        if cancellation_pct >= ONLINE_CANCELLATION_RATE_PCT and not await _already_active(
            "online_cancellation_spike", row.allocated_store_id
        ):
            new_alert = CeoAlert(
                alert_type="online_cancellation_spike",
                severity="high" if cancellation_pct >= 40 else "medium",
                store_id=row.allocated_store_id,
                title="Elevated Online Order Cancellation Rate",
                description=f"{row.cancelled_count} of {row.total_count} online orders today ({cancellation_pct:.0f}%) were cancelled.",
                action_required="Check stock availability, allocation accuracy and fulfilment capacity for this store.",
            )
            db.add(new_alert)
            new_alerts.append(new_alert)

    system_failure_count = (
        await db.execute(
            text("select count(*) from sync_failures where resolved = false and created_at >= current_date - interval '1 day'")
        )
    ).scalar_one()
    if system_failure_count >= SYSTEM_FAILURE_THRESHOLD and not await _already_active("system_failure", None):
        new_alert = CeoAlert(
            alert_type="system_failure",
            severity="critical",
            store_id=None,
            title="Elevated Sync/Device Failures",
            description=f"{system_failure_count} unresolved sync failures logged in the last 24 hours.",
            action_required="Check Sync Health and device connectivity across stores.",
        )
        db.add(new_alert)
        new_alerts.append(new_alert)

    await db.commit()

    if new_alerts:
        await fraud_notify.notify_new_alerts(
            db,
            subject=f"Gropto CEO Control Tower: {len(new_alerts)} new risk alert(s)",
            lines=[f"[{a.severity.upper()}] {a.title} — {a.description}" for a in new_alerts],
        )

    return new_alerts


@router.get("/alerts", response_model=list[CeoAlertOut])
async def list_ceo_alerts(
    store_id: uuid.UUID | None = None,
    status_filter: str = "active",
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("alerts.ceo_alert.view")),
) -> list[CeoAlert]:
    if status_filter == "active":
        await _evaluate_ceo_alerts(db)

    stmt = select(CeoAlert).where(CeoAlert.status == status_filter).order_by(CeoAlert.created_at.desc())
    if store_id:
        require_store_access(store_id, current)
        stmt = stmt.where(CeoAlert.store_id == store_id)
    elif not current.sees_all_stores():
        # Point 13 audit fix: this previously returned every store's alerts
        # to any approval.decide holder with no scoping at all.
        stmt = stmt.where((CeoAlert.store_id.in_(current.store_ids)) | (CeoAlert.store_id.is_(None)))
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.post("/alerts/{alert_id}/assign", response_model=CeoAlertOut)
async def assign_ceo_alert(
    alert_id: uuid.UUID,
    payload: AlertAssign,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("alerts.ceo_alert.assign")),
) -> CeoAlert:
    alert = await db.get(CeoAlert, alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail="CEO alert not found")
    if alert.store_id is not None:
        require_store_access(alert.store_id, current)
    old_assignee = alert.assigned_to
    alert.assigned_to = payload.assignee_id
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=alert.store_id,
        device_id=current.device_id,
        action="ceo_alert.assigned",
        entity_type="ceo_alert",
        entity_id=alert.id,
        old_value={"assigned_to": str(old_assignee) if old_assignee else None},
        new_value={"assigned_to": str(payload.assignee_id)},
    )
    await db.commit()
    await db.refresh(alert)
    return alert


@router.post("/alerts/{alert_id}/resolve", response_model=CeoAlertOut)
async def resolve_ceo_alert(
    alert_id: uuid.UUID,
    payload: AlertResolve,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("alerts.ceo_alert.close")),
) -> CeoAlert:
    alert = await db.get(CeoAlert, alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail="CEO alert not found")
    if alert.store_id is not None:
        require_store_access(alert.store_id, current)
    if alert.status != "active":
        raise HTTPException(status_code=409, detail=f"Alert already {alert.status}")

    alert.status = "resolved"
    alert.resolved_by = current.user_id
    alert.resolved_at = datetime.now(timezone.utc)
    alert.resolution_note = payload.note

    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=alert.store_id,
        device_id=current.device_id,
        action="ceo_alert.resolved",
        entity_type="ceo_alert",
        entity_id=alert.id,
        old_value={"status": "active"},
        new_value={"status": "resolved"},
        reason=payload.note,
    )
    await db.commit()
    await db.refresh(alert)
    return alert


async def _already_logged_today(db: AsyncSession, *, store_id, cashier_id) -> bool:
    existing = (
        await db.execute(
            text(
                """
                select id from suspicious_billing_logs
                where store_id = :store_id and cashier_id = :cashier_id
                  and status = 'open' and created_at::date = current_date
                """
            ),
            {"store_id": store_id, "cashier_id": cashier_id},
        )
    ).scalar_one_or_none()
    return existing is not None


async def _run_suspicious_billing_scan(db: AsyncSession) -> dict:
    """Flags cashiers with an unusually high same-day return count — a real
    repeated-refund pattern, not a fabricated placeholder entry. Point 13
    audit fix: previously had no dedup guard (repeated calls duplicated
    rows for the same cashier/day) and nothing ever read this table back —
    see GET /control-tower/suspicious-billing below. Plain function (not the
    route handler) so the scheduler can call it without faking a Depends."""
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
    new_logs: list[SuspiciousBillingLog] = []
    for row in rows:
        if await _already_logged_today(db, store_id=row.store_id, cashier_id=row.requested_by):
            continue
        log = SuspiciousBillingLog(
            store_id=row.store_id,
            cashier_id=row.requested_by,
            risk_score=min(60 + row.cnt * 5, 99),
            pattern_type="repeated_returns",
            details_json={"return_count": row.cnt, "total_refund_amount": float(row.total_refund)},
        )
        db.add(log)
        new_logs.append(log)
        flagged += 1
    await db.commit()

    if new_logs:
        await fraud_notify.notify_new_alerts(
            db,
            subject=f"Gropto Suspicious Billing Scan: {len(new_logs)} new pattern(s) flagged",
            lines=[f"Risk {l.risk_score}: cashier {l.cashier_id} — {l.pattern_type}" for l in new_logs],
        )

    return {"status": "scan_complete", "flagged_patterns": flagged}


@router.post("/scan-suspicious", response_model=dict)
async def scan_suspicious_billing(
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("fraud.suspicious_billing.create")),
) -> dict:
    return await _run_suspicious_billing_scan(db)


@router.get("/suspicious-billing", response_model=Page[SuspiciousBillingLogOut])
async def list_suspicious_billing(
    store_id: uuid.UUID | None = None,
    status_filter: str = "open",
    limit: int = 20,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("fraud.suspicious_billing.view")),
) -> Page[SuspiciousBillingLogOut]:
    stmt = select(SuspiciousBillingLog).where(SuspiciousBillingLog.status == status_filter)
    if store_id:
        require_store_access(store_id, current)
        stmt = stmt.where(SuspiciousBillingLog.store_id == store_id)
    elif not current.sees_all_stores():
        stmt = stmt.where(SuspiciousBillingLog.store_id.in_(current.store_ids))
    capped_limit = min(limit, 200)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    result = await db.execute(stmt.order_by(SuspiciousBillingLog.created_at.desc()).limit(capped_limit).offset(offset))
    return Page(items=list(result.scalars().all()), total=total, limit=capped_limit, offset=offset)


@router.post("/suspicious-billing/{log_id}/resolve", response_model=SuspiciousBillingLogOut)
async def resolve_suspicious_billing(
    log_id: uuid.UUID,
    payload: AlertResolve,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("fraud.suspicious_billing.close")),
) -> SuspiciousBillingLog:
    log = await db.get(SuspiciousBillingLog, log_id)
    if log is None:
        raise HTTPException(status_code=404, detail="Suspicious billing log not found")
    require_store_access(log.store_id, current)
    if log.status != "open":
        raise HTTPException(status_code=409, detail=f"Log already {log.status}")
    if payload.status not in ("reviewed", "dismissed"):
        raise HTTPException(status_code=400, detail="status must be 'reviewed' or 'dismissed'")

    log.status = payload.status
    log.reviewed = True
    log.resolved_by = current.user_id
    log.resolved_at = datetime.now(timezone.utc)
    log.resolution_note = payload.note

    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=log.store_id,
        device_id=current.device_id,
        action=f"suspicious_billing_log.{payload.status}",
        entity_type="suspicious_billing_log",
        entity_id=log.id,
        old_value={"status": "open"},
        new_value={"status": payload.status},
        reason=payload.note,
    )
    await db.commit()
    await db.refresh(log)
    return log
