import uuid
from contextvars import ContextVar

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import AuditLog

# Set once per request by rate_limit_middleware in app/main.py (the one place
# that already sees every request) and read here — avoids threading a
# `request: Request` parameter through all seventeen write_audit call sites
# across every router just to capture an IP address.
_current_ip: ContextVar[str | None] = ContextVar("_current_ip", default=None)


def set_current_ip(ip: str | None) -> None:
    _current_ip.set(ip)


async def write_audit(
    db: AsyncSession,
    *,
    user_id: uuid.UUID | None,
    role_code: str | None,
    store_id: uuid.UUID | None,
    device_id: uuid.UUID | None,
    action: str,
    entity_type: str,
    entity_id: uuid.UUID | None,
    old_value: dict | None = None,
    new_value: dict | None = None,
    source: str = "api",
    approval_id: uuid.UUID | None = None,
    reason: str | None = None,
) -> None:
    """Always called inside the same transaction as the change it records.
    Never update/delete audit rows through any application path.

    entity_version is this entity's 1-indexed position in its own change
    history (1 for its first-ever audit row, 2 for the next, ...) — computed
    from the existing rows rather than stored on the entity itself, so nothing
    else has to remember to bump it. None when entity_id is None (e.g. a login
    event, which isn't a record with a version history)."""
    entity_version = None
    if entity_id is not None:
        count = await db.scalar(
            select(func.count()).select_from(AuditLog).where(
                AuditLog.entity_type == entity_type, AuditLog.entity_id == entity_id
            )
        )
        entity_version = (count or 0) + 1

    db.add(
        AuditLog(
            user_id=user_id,
            role_code=role_code,
            store_id=store_id,
            device_id=device_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            old_value=old_value,
            new_value=new_value,
            source=source,
            approval_id=approval_id,
            reason=reason,
            ip_address=_current_ip.get(),
            entity_version=entity_version,
        )
    )
