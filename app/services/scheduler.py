import logging
from datetime import datetime, timezone
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import delete, or_

from app.core.database import SessionLocal
from app.models.models import PasswordResetOtp, RefreshToken

logger = logging.getLogger("gropto.scheduler")


async def refresh_rfm_cohorts_job() -> None:
    """Point 3 audit fix: rfm_cohort_snapshots had zero write path anywhere
    (see crm_advanced.py's _compute_rfm_cohorts) — now recomputed daily so
    Cohort Performance reflects current purchase history, not a one-time
    snapshot from whenever the table happened to first be read."""
    from app.api.v1.crm_advanced import refresh_rfm_cohorts

    async with SessionLocal() as db:
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

async def refresh_abc_xyz_job() -> None:
    """Point 7 audit fix: ABC/XYZ classification was computed once per store,
    lazily, and never refreshed afterward."""
    from app.api.v1.inventory_intelligence import refresh_all_abc_xyz

    async with SessionLocal() as db:
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

    async with SessionLocal() as db:
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
        scheduler.start()
        logger.info("APScheduler started successfully.")


def stop_scheduler() -> None:
    """Shuts down the APScheduler."""
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("APScheduler stopped.")
