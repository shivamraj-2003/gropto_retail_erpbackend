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
        scheduler.start()
        logger.info("APScheduler started successfully.")


def stop_scheduler() -> None:
    """Shuts down the APScheduler."""
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("APScheduler stopped.")
