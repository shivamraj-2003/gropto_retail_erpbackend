"""Point 13 audit fix: fraud/CEO/suspicious-billing alerts previously had no
notification path at all — a reviewer only ever learned of a new alert by
happening to open the relevant screen. Sends one real email (via the
existing Resend client, honestly no-op if RESEND_API_KEY is unset — same
is_configured() pattern as every other provider in this codebase) per
fraud.manage holder, batched per scan/evaluation run rather than per alert
row, so a run that raises 20 alerts sends one email, not 20."""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import Permission, Role, RolePermission, User
from app.services import email as email_service

logger = logging.getLogger("gropto.fraud_notify")


async def _recipients(db: AsyncSession, *, permission_code: str) -> list[str]:
    rows = (
        await db.execute(
            select(User.email)
            .join(Role, Role.id == User.role_id)
            .join(RolePermission, RolePermission.role_id == Role.id)
            .join(Permission, Permission.id == RolePermission.permission_id)
            .where(Permission.code == permission_code, User.is_active.is_(True), User.email.is_not(None))
        )
    ).scalars().all()
    return [e for e in rows if e]


async def notify_new_alerts(db: AsyncSession, *, subject: str, lines: list[str]) -> int:
    """Returns the number of recipients actually emailed (0 if unconfigured
    or there's nothing new to report — never fabricates a "sent" count)."""
    if not lines:
        return 0
    if not email_service.is_configured():
        logger.warning(f"Fraud/CEO alert notification skipped (Resend not configured): {subject}")
        return 0

    recipients = await _recipients(db, permission_code="fraud.manage")
    if not recipients:
        return 0

    html = "<p>" + "</p><p>".join(lines) + "</p>"
    sent = 0
    for to in recipients:
        ok, _ = await email_service.send_email(to, subject, html)
        if ok:
            sent += 1
        else:
            logger.warning(f"Fraud/CEO alert email failed to {to}")
    return sent
