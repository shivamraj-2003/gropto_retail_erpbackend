import functools
import logging
import os
from datetime import datetime, timezone
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import delete, or_

from app.core.kv import get_kv, key
from app.core.database import ReportingSessionLocal, SessionLocal
from app.models.models import PasswordResetOtp, RefreshToken

logger = logging.getLogger("gropto.scheduler")


async def refresh_rfm_cohorts_job() -> None:
    """Point 3 audit fix: rfm_cohort_snapshots had zero write path anywhere
    (see crm_advanced.py's _compute_rfm_cohorts) — now recomputed daily so
    Cohort Performance reflects current purchase history, not a one-time
    snapshot from whenever the table happened to first be read."""
    from app.api.v1.crm_advanced import refresh_rfm_cohorts

    async with ReportingSessionLocal() as db:
        try:
            await refresh_rfm_cohorts(db)
        except Exception as e:
            await db.rollback()
            logger.error(f"Error during scheduled RFM cohort refresh: {e}")


async def evaluate_ceo_alerts_job() -> None:
    """Point 3 audit fix: _evaluate_ceo_alerts() used to run only as a side
    effect of someone hitting GET /control-tower/alerts, so the CEO Command
    Center's active-alert count could sit stale/zero for hours despite a real,
    current risk condition. Runs the same evaluation on a schedule instead."""
    from app.api.v1.control_tower import _evaluate_ceo_alerts

    async with SessionLocal() as db:
        try:
            await _evaluate_ceo_alerts(db)
            await db.commit()
        except Exception as e:
            await db.rollback()
            logger.error(f"Error during scheduled CEO alert evaluation: {e}")

async def run_fraud_scan_job() -> None:
    """Point 13 audit fix: run_fraud_scan's own docstring admitted this was
    "on demand ... call from a scheduler in production" and never was —
    fraud_alerts only ever populated if a Super Admin remembered to click
    the manual scan button. Now runs on the same cadence as CEO alerts."""
    from app.services import fraud as fraud_service
    from app.services import fraud_notify

    async with SessionLocal() as db:
        try:
            alerts = await fraud_service.run_fraud_scan(db)
            await db.commit()
            if alerts:
                await fraud_notify.notify_new_alerts(
                    db,
                    subject=f"Gropto Fraud Scan: {len(alerts)} new alert(s)",
                    lines=[f"[{a.severity.upper()}] {a.rule_code.replace('_', ' ')} — {a.details}" for a in alerts],
                )
        except Exception as e:
            await db.rollback()
            logger.error(f"Error during scheduled fraud scan: {e}")


async def scan_suspicious_billing_job() -> None:
    """Point 13 audit fix: scan_suspicious_billing was reachable only by a
    direct API call nobody in the UI ever makes — dead code end-to-end.
    Scheduled the same way as the fraud scan so suspicious_billing_logs
    actually gets populated in production."""
    from app.api.v1.control_tower import _run_suspicious_billing_scan

    async with SessionLocal() as db:
        try:
            await _run_suspicious_billing_scan(db)
        except Exception as e:
            await db.rollback()
            logger.error(f"Error during scheduled suspicious billing scan: {e}")


async def refresh_abc_xyz_job() -> None:
    """Point 7 audit fix: ABC/XYZ classification was computed once per store,
    lazily, and never refreshed afterward."""
    from app.api.v1.inventory_intelligence import refresh_all_abc_xyz

    async with ReportingSessionLocal() as db:
        try:
            count = await refresh_all_abc_xyz(db)
            logger.info(f"ABC/XYZ refresh complete: {count} product snapshot(s) updated.")
        except Exception as e:
            await db.rollback()
            logger.error(f"Error during scheduled ABC/XYZ refresh: {e}")


async def refresh_vendor_performance_job() -> None:
    """Point 6 audit fix: vendor_performance_snapshots was never written to
    by any code despite its own docstring claiming a nightly refresh job."""
    from app.services.vendor_performance import compute_vendor_performance

    async with ReportingSessionLocal() as db:
        try:
            count = await compute_vendor_performance(db)
            await db.commit()
            logger.info(f"Vendor performance refresh complete: {count} vendor snapshot(s) updated.")
        except Exception as e:
            await db.rollback()
            logger.error(f"Error during scheduled vendor performance refresh: {e}")


async def execute_scheduled_price_changes_job() -> None:
    """Point 9 audit fix: ScheduledPriceChange.status could sit at "approved"
    forever — nothing ever applied an approved change to the Product or
    advanced it to "executed" once effective_at arrived."""
    from sqlalchemy import select
    from app.models.models import Product
    from app.models.models_phase4 import ScheduledPriceChange
    from app.services.audit import write_audit

    async with SessionLocal() as db:
        try:
            now = datetime.now(timezone.utc)
            rows = (
                await db.execute(
                    select(ScheduledPriceChange).where(
                        ScheduledPriceChange.status == "approved",
                        ScheduledPriceChange.effective_at <= now,
                    )
                )
            ).scalars().all()
            executed = 0
            for sp_change in rows:
                product = await db.get(Product, sp_change.product_id)
                if product is None:
                    sp_change.status = "stale"
                    continue
                old_sp, old_mrp = float(product.selling_price), float(product.mrp)
                product.selling_price = sp_change.new_sp
                product.mrp = sp_change.new_mrp
                product.revision = (product.revision or 0) + 1
                sp_change.status = "executed"
                await write_audit(
                    db,
                    user_id=None,
                    role_code=None,
                    store_id=None,
                    device_id=None,
                    action="scheduled_price_change.executed",
                    entity_type="product",
                    entity_id=product.id,
                    old_value={"selling_price": old_sp, "mrp": old_mrp},
                    new_value={"selling_price": float(sp_change.new_sp), "mrp": float(sp_change.new_mrp)},
                    source="scheduler",
                )
                executed += 1
            await db.commit()
            if executed:
                logger.info(f"Executed {executed} scheduled price change(s).")
        except Exception as e:
            await db.rollback()
            logger.error(f"Error during scheduled price change execution: {e}")


async def expire_loyalty_points_job() -> None:
    """Point 9 audit fix: loyalty points never expired under any code path."""
    from app.services.loyalty import expire_lapsed_points

    async with SessionLocal() as db:
        try:
            count = await expire_lapsed_points(db)
            await db.commit()
            if count:
                logger.info(f"Loyalty point expiry: {count} lot(s) expired.")
        except Exception as e:
            await db.rollback()
            logger.error(f"Error during scheduled loyalty point expiry: {e}")


async def retry_pending_einvoices_job() -> None:
    """Point 10 audit fix: no e-invoice retry mechanism existed (nothing
    did, because e-invoicing itself didn't exist). Always a safe no-op when
    GSP credentials aren't configured — see services/einvoice.py."""
    from app.services.einvoice import queue_required_einvoices, retry_failed_einvoices

    async with SessionLocal() as db:
        try:
            queued = await queue_required_einvoices(db)
            if queued:
                logger.info(f"E-invoice queue: {queued} B2B invoice(s) queued for stores where e-invoicing applies.")
            count = await retry_failed_einvoices(db)
            await db.commit()
            if count:
                logger.info(f"E-invoice retry: {count} invoice(s) re-attempted.")
        except Exception as e:
            await db.rollback()
            logger.error(f"Error during scheduled e-invoice retry: {e}")


async def refresh_budget_actuals_job() -> None:
    """Point 10 audit fix: StoreBudget.actual_opex/actual_capex had no
    computation path at all."""
    from datetime import date

    from app.services.budget import refresh_actuals

    today = date.today()
    financial_year = today.year if today.month >= 4 else today.year - 1
    async with ReportingSessionLocal() as db:
        try:
            count = await refresh_actuals(db, financial_year=financial_year, month=today.month)
            await db.commit()
            if count:
                logger.info(f"Budget actuals refresh: {count} store budget(s) updated.")
        except Exception as e:
            await db.rollback()
            logger.error(f"Error during scheduled budget actuals refresh: {e}")


scheduler = AsyncIOScheduler()


async def purge_expired_auth_data() -> None:
    """Deletes expired or revoked refresh tokens and expired/used OTPs from the database."""
    now = datetime.now(timezone.utc)
    async with SessionLocal() as db:
        try:
            # Purge refresh tokens that are either revoked or expired
            token_result = await db.execute(
                delete(RefreshToken).where(
                    or_(
                        RefreshToken.revoked.is_(True),
                        RefreshToken.expires_at < now,
                    )
                )
            )
            
            # Purge password reset OTPs that are either used or expired
            otp_result = await db.execute(
                delete(PasswordResetOtp).where(
                    or_(
                        PasswordResetOtp.used.is_(True),
                        PasswordResetOtp.expires_at < now,
                    )
                )
            )
            
            await db.commit()
            logger.info(
                f"Auth cleanup complete: purged {token_result.rowcount} refresh tokens and {otp_result.rowcount} OTP records."
            )
        except Exception as e:
            await db.rollback()
            logger.error(f"Error during auth cleanup task: {e}")


async def refresh_clv_snapshots_job() -> None:
    """Point 19 fix: clv_snapshots were only recomputed when someone pressed
    refresh in CRM, so CLV/churn figures could be arbitrarily stale."""
    from app.services.clv import compute_clv_snapshots

    async with ReportingSessionLocal() as db:
        try:
            await compute_clv_snapshots(db)
        except Exception as e:
            await db.rollback()
            logger.error(f"Error during scheduled CLV refresh: {e}")


def _exclusive(job_id: str, fn, lock_seconds: int):
    """Every API worker runs its own scheduler; a Redis lock (SET NX EX) makes
    sure only one of them actually executes each tick of each job. Held for
    most of the interval so a worker whose clock fires slightly later skips
    too. Without Redis the in-memory lock always succeeds (single process)."""

    @functools.wraps(fn)
    async def run() -> None:
        if not await get_kv().set(key("job", job_id), str(os.getpid()), ttl_seconds=lock_seconds, nx=True):
            return
        await fn()

    return run


def start_scheduler() -> None:
    """Starts the APScheduler background jobs."""
    if not scheduler.running:
        # Run cleanup job every hour
        scheduler.add_job(
            purge_expired_auth_data,
            "interval",
            hours=1,
            id="purge_expired_auth_data",
            replace_existing=True,
        )
        scheduler.add_job(
            evaluate_ceo_alerts_job,
            "interval",
            minutes=15,
            id="evaluate_ceo_alerts",
            replace_existing=True,
        )
        scheduler.add_job(
            run_fraud_scan_job,
            "interval",
            minutes=30,
            id="run_fraud_scan",
            replace_existing=True,
        )
        scheduler.add_job(
            scan_suspicious_billing_job,
            "interval",
            minutes=30,
            id="scan_suspicious_billing",
            replace_existing=True,
        )
        scheduler.add_job(
            refresh_rfm_cohorts_job,
            "interval",
            hours=24,
            id="refresh_rfm_cohorts",
            replace_existing=True,
        )
        scheduler.add_job(
            refresh_vendor_performance_job,
            "interval",
            hours=24,
            id="refresh_vendor_performance",
            replace_existing=True,
        )
        scheduler.add_job(
            refresh_abc_xyz_job,
            "interval",
            hours=24,
            id="refresh_abc_xyz",
            replace_existing=True,
        )
        scheduler.add_job(
            execute_scheduled_price_changes_job,
            "interval",
            minutes=15,
            id="execute_scheduled_price_changes",
            replace_existing=True,
        )
        scheduler.add_job(
            expire_loyalty_points_job,
            "interval",
            hours=24,
            id="expire_loyalty_points",
            replace_existing=True,
        )
        scheduler.add_job(
            retry_pending_einvoices_job,
            "interval",
            hours=1,
            id="retry_pending_einvoices",
            replace_existing=True,
        )
        scheduler.add_job(
            refresh_budget_actuals_job,
            "interval",
            hours=24,
            id="refresh_budget_actuals",
            replace_existing=True,
        )
        scheduler.add_job(
            refresh_clv_snapshots_job,
            "interval",
            hours=24,
            id="refresh_clv_snapshots",
            replace_existing=True,
        )
        for job in scheduler.get_jobs():
            interval = int(job.trigger.interval.total_seconds())
            job.modify(func=_exclusive(job.id, job.func, max(int(interval * 0.8), 30)))
        scheduler.start()
        logger.info("APScheduler started successfully.")


def stop_scheduler() -> None:
    """Shuts down the APScheduler."""
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("APScheduler stopped.")
