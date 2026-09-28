import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, get_current_user, require_permission, require_store_access
from app.core.database import get_db
from app.core.security import (
    create_access_token,
    hash_password,
    hash_refresh_token,
    new_refresh_token,
    refresh_token_expiry,
    verify_password,
)
from app.models.models import Device, PasswordResetOtp, RefreshToken, Role, Store, User, UserStore
from app.schemas.schemas import (
    ChangePasswordIn,
    DeviceOut,
    ForgotPasswordIn,
    LoginRequest,
    RefreshRequest,
    ResetPasswordWithOtpIn,
    StoreCredentialRow,
    TokenResponse,
)
from app.services import email as email_service
from app.services.audit import write_audit
from app.services.rate_limit import is_login_locked, record_login_failure, record_login_success

OTP_EXPIRY_MINUTES = 10

router = APIRouter(prefix="/auth", tags=["auth"])


async def _load_user_store_ids(db: AsyncSession, user_id: uuid.UUID) -> list[uuid.UUID]:
    result = await db.execute(select(UserStore.store_id).where(UserStore.user_id == user_id))
    return list(result.scalars().all())


@router.post("/login", response_model=TokenResponse)
async def login(payload: LoginRequest, db: AsyncSession = Depends(get_db)) -> TokenResponse:
    if not payload.email and not payload.phone:
        raise HTTPException(status_code=400, detail="email or phone required")

    identifier = payload.email or payload.phone or ""
    remaining = is_login_locked(identifier, payload.device_fingerprint)
    if remaining is not None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many failed attempts. Try again in {int(remaining // 60) + 1} minute(s).",
        )

    stmt = select(User).where(
        (User.email == payload.email) if payload.email else (User.phone == payload.phone)
    )
    user = (await db.execute(stmt)).scalar_one_or_none()
    if user is None or not user.is_active or not verify_password(payload.password, user.password_hash):
        record_login_failure(identifier, payload.device_fingerprint)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    record_login_success(identifier, payload.device_fingerprint)

    role = await db.get(Role, user.role_id)

    device_stmt = select(Device).where(Device.fingerprint == payload.device_fingerprint)
    device = (await db.execute(device_stmt)).scalar_one_or_none()

    if device is None:
        # Login is email+password only — a device new to the system is
        # recorded and activated in the same step, not held pending. The
        # Device row still exists (bill numbering, cash sessions, and sync
        # are all scoped to it), it's just no longer a login gate.
        store_ids = await _load_user_store_ids(db, user.id)
        target_store_id = store_ids[0] if store_ids else (await db.execute(select(Store.id).limit(1))).scalar_one_or_none()
        if target_store_id is None:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No store exists to register this device against")
        device = Device(
            store_id=target_store_id,
            code=f"AUTO-{secrets.token_hex(2).upper()}",
            fingerprint=payload.device_fingerprint,
            status="active",
        )
        db.add(device)
        await db.flush()

    if device.status == "revoked":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Device has been revoked")

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


@router.post("/change-password", status_code=204)
async def change_password(
    payload: ChangePasswordIn,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    user = await db.get(User, current.user_id)
    if user is None or not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Current password is incorrect")
    user.password_hash = hash_password(payload.new_password)
    # Every other device/session must re-login with the new password.
    await db.execute(
        RefreshToken.__table__.update().where(RefreshToken.user_id == user.id).values(revoked=True)
    )
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=None,
        device_id=current.device_id,
        action="user.password_changed",
        entity_type="user",
        entity_id=user.id,
    )
    await db.commit()


@router.post("/forgot-password", status_code=200)
async def forgot_password(payload: ForgotPasswordIn, db: AsyncSession = Depends(get_db)) -> dict:
    """When Resend is configured (RESEND_API_KEY in .env), this emails a
    6-digit OTP the account holder submits to POST /auth/reset-password-with-otp
    to set their own new password — fully self-service. Without it, this
    falls back to flagging the request so an Admin/Super Admin sees it and
    resets the password directly (POST /users/{id}/reset-password). Always
    returns the same generic message regardless of whether the email exists,
    so this can't be used to enumerate accounts."""
    stmt = select(User).where(User.email == payload.email)
    user = (await db.execute(stmt)).scalar_one_or_none()

    if user is not None and email_service.is_configured():
        # Invalidate any still-live OTPs from an earlier request so only the
        # newest code works.
        await db.execute(
            PasswordResetOtp.__table__.update()
            .where(PasswordResetOtp.user_id == user.id, PasswordResetOtp.used.is_(False))
            .values(used=True)
        )
        code = f"{secrets.randbelow(1_000_000):06d}"
        db.add(
            PasswordResetOtp(
                user_id=user.id,
                code_hash=hash_password(code),
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=OTP_EXPIRY_MINUTES),
            )
        )
        await email_service.send_email(
            payload.email,
            "Your Gropto ERP password reset code",
            f"<p>Your verification code is <strong>{code}</strong>. It expires in {OTP_EXPIRY_MINUTES} minutes.</p>"
            "<p>If you didn't request this, you can ignore this email.</p>",
        )
        await write_audit(
            db,
            user_id=user.id,
            role_code=None,
            store_id=None,
            device_id=None,
            action="auth.forgot_password_otp_sent",
            entity_type="user",
            entity_id=user.id,
        )
        await db.commit()
        return {"message": "If that account exists, a verification code has been emailed to it.", "otp_sent": True}

    if user is not None:
        await write_audit(
            db,
            user_id=user.id,
            role_code=None,
            store_id=None,
            device_id=None,
            action="auth.forgot_password_requested",
            entity_type="user",
            entity_id=user.id,
        )
        await db.commit()
    return {
        "message": "If that account exists, your store's Admin or Super Admin has been notified to reset your password.",
        "otp_sent": False,
    }


@router.post("/reset-password-with-otp", status_code=204)
async def reset_password_with_otp(payload: ResetPasswordWithOtpIn, db: AsyncSession = Depends(get_db)) -> None:
    remaining = is_login_locked(payload.email, "otp-reset")
    if remaining is not None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many attempts. Try again in {int(remaining // 60) + 1} minute(s).",
        )

    stmt = select(User).where(User.email == payload.email)
    user = (await db.execute(stmt)).scalar_one_or_none()
    if user is None:
        record_login_failure(payload.email, "otp-reset")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired code")

    otp_stmt = select(PasswordResetOtp).where(
        PasswordResetOtp.user_id == user.id,
        PasswordResetOtp.used.is_(False),
        PasswordResetOtp.expires_at > datetime.now(timezone.utc),
    )
    candidates = (await db.execute(otp_stmt)).scalars().all()
    match = next((c for c in candidates if verify_password(payload.otp, c.code_hash)), None)
    if match is None:
        record_login_failure(payload.email, "otp-reset")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired code")

    record_login_success(payload.email, "otp-reset")
    match.used = True
    user.password_hash = hash_password(payload.new_password)
    await db.execute(
        RefreshToken.__table__.update().where(RefreshToken.user_id == user.id).values(revoked=True)
    )
    await write_audit(
        db,
        user_id=user.id,
        role_code=None,
        store_id=None,
        device_id=None,
        action="user.password_reset_via_otp",
        entity_type="user",
        entity_id=user.id,
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
    """Pre-registers a device before it ever logs in. Created active
    immediately — login is email+password only, there's no approval gate to
    clear afterward."""
    device = Device(store_id=store_id, code=code, fingerprint=fingerprint, status="active")
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
    return {"device_id": str(device.id)}


@router.get("/devices", response_model=list[DeviceOut])
async def list_devices(
    status_filter: str | None = None,
    current: CurrentUser = Depends(require_permission("device.manage")),
    db: AsyncSession = Depends(get_db),
) -> list[Device]:
    stmt = select(Device)
    if status_filter:
        stmt = stmt.where(Device.status == status_filter)
    if current.role_code != "super_admin":
        stmt = stmt.where(Device.store_id.in_(current.store_ids))
    result = await db.execute(stmt.order_by(Device.created_at.desc()))
    return list(result.scalars().all())


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


@router.get("/store-credentials", response_model=list[StoreCredentialRow])
async def store_credentials(
    store_id: uuid.UUID,
    current: CurrentUser = Depends(require_permission("inventory.view")),
    db: AsyncSession = Depends(get_db),
) -> list[User]:
    """The password-hash mirror an activated device caches locally so login still
    works with no connectivity — the offline counterpart to online /auth/login.
    Scoped to one store: any authenticated user with access to that store can
    pull it (a device needs to serve every cashier who might work that till, not
    just whoever last logged in online), never cross-store. The hash itself is
    Argon2id, identical to what the device verifies against offline — this
    endpoint ships the hash, never the plaintext password."""
    require_store_access(store_id, current)
    stmt = (
        select(User, Role.code)
        .join(UserStore, UserStore.user_id == User.id)
        .join(Role, Role.id == User.role_id)
        .where(UserStore.store_id == store_id, User.is_active.is_(True))
    )
    rows = (await db.execute(stmt)).all()
    return [
        StoreCredentialRow(
            id=u.id,
            email=u.email,
            phone=u.phone,
            full_name=u.full_name,
            password_hash=u.password_hash,
            role_code=role_code,
            is_active=u.is_active,
        )
        for u, role_code in rows
    ]


@router.get("/me")
async def me(current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    device_code = None
    if current.device_id:
        device = await db.get(Device, current.device_id)
        device_code = device.code if device else None

    primary_store_code = None
    if current.store_ids:
        store = await db.get(Store, current.store_ids[0])
        primary_store_code = store.code if store else None

    return {
        "user_id": str(current.user_id),
        "role": current.role_code,
        "stores": [str(s) for s in current.store_ids],
        "device_id": str(current.device_id) if current.device_id else None,
        # Needed on-device to format bill numbers as store_code-device_code-seq
        # (§4 of the plan) rather than an opaque sequence.
        "device_code": device_code,
        "primary_store_code": primary_store_code,
    }
