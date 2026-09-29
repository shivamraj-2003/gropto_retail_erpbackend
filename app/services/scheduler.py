import logging
from datetime import datetime, timezone
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import delete, or_

from app.core.database import SessionLocal
from app.models.models import PasswordResetOtp, RefreshToken

logger = logging.getLogger("gropto.scheduler")

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
        scheduler.start()
        logger.info("APScheduler started successfully.")


def stop_scheduler() -> None:
    """Shuts down the APScheduler."""
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("APScheduler stopped.")
