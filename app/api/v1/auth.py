import secrets
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, get_current_user, require_permission
from app.core.database import get_db
from app.core.security import (
    create_access_token,
    hash_refresh_token,
    new_refresh_token,
    refresh_token_expiry,
    verify_password,
)
from app.models.models import Device, RefreshToken, Role, User, UserStore
from app.schemas.schemas import LoginRequest, RefreshRequest, TokenResponse
from app.services.audit import write_audit

router = APIRouter(prefix="/auth", tags=["auth"])


async def _load_user_store_ids(db: AsyncSession, user_id: uuid.UUID) -> list[uuid.UUID]:
    result = await db.execute(select(UserStore.store_id).where(UserStore.user_id == user_id))
    return list(result.scalars().all())


@router.post("/login", response_model=TokenResponse)
async def login(payload: LoginRequest, db: AsyncSession = Depends(get_db)) -> TokenResponse:
    if not payload.email and not payload.phone:
        raise HTTPException(status_code=400, detail="email or phone required")

    stmt = select(User).where(
        (User.email == payload.email) if payload.email else (User.phone == payload.phone)
    )
    user = (await db.execute(stmt)).scalar_one_or_none()
    if user is None or not user.is_active or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    role = await db.get(Role, user.role_id)

    device_stmt = select(Device).where(Device.fingerprint == payload.device_fingerprint)
    device = (await db.execute(device_stmt)).scalar_one_or_none()

    if device is None:
        # A device must be pre-registered by a Super Admin (POST /auth/devices/register)
        # before it can ever log in — logging in never silently creates a device.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Device not registered. Ask a Super Admin to register this device first.",
        )

    if device.status == "revoked":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Device has been revoked")

    if device.status == "pending":
        if not payload.device_activation_code or payload.device_activation_code != device.activation_code:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid activation code")
        device.status = "active"
        device.activation_code = None

    if role.code != "super_admin":
        store_ids = await _load_user_store_ids(db, user.id)
        if device.store_id and device.store_id not in store_ids:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User not assigned to this store")
    device.last_seen_at = datetime.now(timezone.utc)

    store_ids = [str(s) for s in await _load_user_store_ids(db, user.id)]
    access_token = create_access_token(
        user_id=user.id, role_code=role.code, store_ids=store_ids, device_id=str(device.id)
    )
    raw_refresh, refresh_hash = new_refresh_token()
    db.add(
        RefreshToken(
            user_id=user.id,
            device_id=device.id,
            token_hash=refresh_hash,
            family_id=uuid.uuid4(),
            expires_at=refresh_token_expiry(),
        )
    )
    await write_audit(
        db,
        user_id=user.id,
        role_code=role.code,
        store_id=device.store_id,
        device_id=device.id,
        action="auth.login",
        entity_type="user",
        entity_id=user.id,
    )
    await db.commit()
    return TokenResponse(access_token=access_token, refresh_token=raw_refresh)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(payload: RefreshRequest, db: AsyncSession = Depends(get_db)) -> TokenResponse:
    token_hash = hash_refresh_token(payload.refresh_token)
    stmt = select(RefreshToken).where(RefreshToken.token_hash == token_hash)
    token_row = (await db.execute(stmt)).scalar_one_or_none()

    if token_row is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token")

    if token_row.revoked:
        # Reuse of a rotated token: revoke the whole family and force re-login everywhere.
        await db.execute(
            RefreshToken.__table__.update()
            .where(RefreshToken.family_id == token_row.family_id)
            .values(revoked=True)
        )
        await db.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh token reuse detected")

    if token_row.expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh token expired")

    device = await db.get(Device, token_row.device_id) if token_row.device_id else None
    if device and device.fingerprint != payload.device_fingerprint:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Device mismatch")
    if device and device.status != "active":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Device no longer active")

    user = await db.get(User, token_row.user_id)
    role = await db.get(Role, user.role_id)
    store_ids = [str(s) for s in await _load_user_store_ids(db, user.id)]

    # Rotate: revoke old, issue new in the same family.
    token_row.revoked = True
    raw_refresh, refresh_hash = new_refresh_token()
    db.add(
        RefreshToken(
            user_id=user.id,
            device_id=token_row.device_id,
            token_hash=refresh_hash,
            family_id=token_row.family_id,
            expires_at=refresh_token_expiry(),
        )
    )
    access_token = create_access_token(
        user_id=user.id,
        role_code=role.code,
        store_ids=store_ids,
        device_id=str(token_row.device_id) if token_row.device_id else None,
    )
    await db.commit()
    return TokenResponse(access_token=access_token, refresh_token=raw_refresh)


@router.post("/logout", status_code=204)
async def logout(payload: RefreshRequest, db: AsyncSession = Depends(get_db)) -> None:
    token_hash = hash_refresh_token(payload.refresh_token)
    await db.execute(
        RefreshToken.__table__.update().where(RefreshToken.token_hash == token_hash).values(revoked=True)
    )
    await db.commit()


@router.post("/devices/register", status_code=201)
async def register_device(
    store_id: uuid.UUID,
    code: str,
    fingerprint: str,
    current: CurrentUser = Depends(require_permission("device.manage")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    activation_code = secrets.token_hex(4)
    device = Device(store_id=store_id, code=code, fingerprint=fingerprint, status="pending", activation_code=activation_code)
    db.add(device)
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=store_id,
        device_id=None,
        action="device.registered",
        entity_type="device",
        entity_id=None,
        new_value={"code": code, "fingerprint": fingerprint},
    )
    await db.commit()
    return {"device_id": str(device.id), "activation_code": activation_code}


@router.post("/devices/{device_id}/revoke", status_code=204)
async def revoke_device(
    device_id: uuid.UUID,
    current: CurrentUser = Depends(require_permission("device.manage")),
    db: AsyncSession = Depends(get_db),
) -> None:
    device = await db.get(Device, device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found")
    device.status = "revoked"
    await db.execute(
        RefreshToken.__table__.update().where(RefreshToken.device_id == device_id).values(revoked=True)
    )
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=device.store_id,
        device_id=device_id,
        action="device.revoked",
        entity_type="device",
        entity_id=device_id,
    )
    await db.commit()


@router.get("/me")
async def me(current: CurrentUser = Depends(get_current_user)) -> dict:
    return {
        "user_id": str(current.user_id),
        "role": current.role_code,
        "stores": [str(s) for s in current.store_ids],
        "device_id": str(current.device_id) if current.device_id else None,
    }
