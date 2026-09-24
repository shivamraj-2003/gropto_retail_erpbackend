import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import AuditLog


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
) -> None:
    """Always called inside the same transaction as the change it records.
    Never update/delete audit rows through any application path."""
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
        )
    )
