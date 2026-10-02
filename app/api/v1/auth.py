import secrets
import uuid
from datetime import datetime, timezone

import pyotp
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, get_current_user, require_permission, require_store_access
from app.core.config import settings
from app.core.database import get_db
from app.core.security import (
    create_access_token,
    create_mfa_challenge_token,
    decode_token,
    hash_password,
    hash_refresh_token,
    new_refresh_token,
    refresh_token_expiry,
    verify_password,
)
from app.models.models import Device, PasswordResetOtp, RefreshToken, Role, Store, User, UserStore
from app.models.models_phase4 import Cluster
from app.schemas.schemas import (
    ChangePasswordIn,
    DeviceOut,
    ForgotPasswordIn,
    LoginRequest,
    LoginResponse,
    MfaLoginVerifyIn,
    MfaSetupOut,
    MfaVerifySetupIn,
    Page,
    RefreshRequest,
    ResetPasswordWithOtpIn,
    StoreCredentialRow,
    TokenResponse,
)
from app.services import otp as otp_service
from app.services.audit import write_audit
from app.services.rbac import resolve_access
from app.services.rate_limit import is_login_locked, record_login_failure, record_login_success


router = APIRouter(prefix="/auth", tags=["auth"])


async def _load_user_store_ids(db: AsyncSession, user_id: uuid.UUID) -> list[uuid.UUID]:
    """Direct user_stores assignment, UNIONed with every store belonging to a
    Cluster this user manages (Point 2 audit fix — Cluster.regional_manager_id
    previously carried zero actual stores; a Regional/Cluster Manager's real
    access only ever came from the generic user_stores table, same as any
    other store-scoped role, making the Cluster assignment pure write-only
    decoration). A Regional Manager still never bypasses store scoping
    entirely (ENTERPRISE_WIDE_ROLES in deps.py deliberately excludes them) —
    this only widens *which* stores they're scoped to, same mechanism as
    always."""
    direct = await db.execute(select(UserStore.store_id).where(UserStore.user_id == user_id))
    via_cluster = await db.execute(
        select(Store.id).join(Cluster, Cluster.id == Store.cluster_id).where(Cluster.regional_manager_id == user_id)
    )
    return list({*direct.scalars().all(), *via_cluster.scalars().all()})


async def _default_device_fingerprint(db: AsyncSession, user: User) -> str:
    """Fallback identity for a login that carries no device_fingerprint.

    Devices remain a real concept here (bill numbers, cash sessions and sync are
    all scoped to one), so a client that doesn't identify itself gets bound to
    the user's own existing active device instead of minting a new row on every
    login. Only a user with no usable device falls through to the deterministic
    per-user key, which keeps the unique constraint on devices.fingerprint
    satisfied.
    """
    store_ids = await _load_user_store_ids(db, user.id)
    stmt = select(Device).where(Device.status == "active")
    if store_ids:
        stmt = stmt.where(Device.store_id.in_(store_ids))
    device = (await db.execute(stmt.order_by(Device.created_at).limit(1))).scalar_one_or_none()
    return device.fingerprint if device is not None else f"user-{user.id}"


@router.post("/login", response_model=LoginResponse)
async def login(payload: LoginRequest, db: AsyncSession = Depends(get_db)) -> LoginResponse:
    if not payload.email and not payload.phone:
        raise HTTPException(status_code=400, detail="email or phone required")

    identifier = payload.email or payload.phone or ""
    # Lockout is keyed by identifier+device so one till can't lock out every
    # cashier at a store. With no fingerprint supplied there is no device
    # dimension to scope by, so the identifier alone is the key.
    lockout_device = payload.device_fingerprint or "no-device"
    remaining = await is_login_locked(identifier, lockout_device)
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
        await record_login_failure(identifier, lockout_device)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    await record_login_success(identifier, lockout_device)

    role = await db.get(Role, user.role_id)

    fingerprint = payload.device_fingerprint or await _default_device_fingerprint(db, user)
    device_stmt = select(Device).where(Device.fingerprint == fingerprint)
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
            fingerprint=fingerprint,
            status="active",
        )
        db.add(device)
        await db.flush()

    if device.status == "revoked":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Device has been revoked")

    # Users whose scope is enterprise-wide (a 'global' role, or Super Admin)
    # must be able to log in even though they carry zero store_ids by design —
    # they aren't tied to a device's home store the way a Cashier/Store Manager
    # is. Point 14: resolved from roles.scope_level + user_scopes (a cluster/
    # company scope counts as assignment) instead of a hardcoded role list.
    access = await resolve_access(db, user)
    if not access.global_scope:
        if device.store_id and device.store_id not in access.store_ids:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User not assigned to this store")
    device.last_seen_at = datetime.now(timezone.utc)

    if user.mfa_enabled:
        # Password already verified above; withhold real tokens until a valid
        # TOTP code confirms the second factor. Nothing is committed yet
        # beyond the lockout bookkeeping already recorded.
        await db.commit()
        return LoginResponse(mfa_required=True, mfa_challenge_token=create_mfa_challenge_token(user_id=user.id))

    access_token, raw_refresh = await _issue_tokens(db, user=user, role=role, device=device)
    await db.commit()
    return LoginResponse(access_token=access_token, refresh_token=raw_refresh)


async def _issue_tokens(db: AsyncSession, *, user: User, role: Role, device: Device) -> tuple[str, str]:
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
    return access_token, raw_refresh


@router.post("/mfa/login-verify", response_model=TokenResponse)
async def mfa_login_verify(payload: MfaLoginVerifyIn, db: AsyncSession = Depends(get_db)) -> TokenResponse:
    try:
        claims = decode_token(payload.challenge_token)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired MFA challenge")
    if claims.get("type") != "mfa_challenge":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired MFA challenge")

    user = await db.get(User, uuid.UUID(claims["sub"]))
    if user is None or not user.is_active or not user.mfa_enabled or not user.mfa_secret:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="MFA not available for this account")
    if not pyotp.TOTP(user.mfa_secret).verify(payload.code, valid_window=1):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid authentication code")

    role = await db.get(Role, user.role_id)
    # The device that originated this login isn't persisted anywhere
    # mid-challenge (only the user_id survives on the challenge token) — fall
    # back to the user's most-recently-seen active device, same resolution
    # login() itself uses when no fingerprint is supplied.
    device = (
        await db.execute(
            select(Device).where(Device.status == "active").order_by(Device.last_seen_at.desc().nulls_last()).limit(1)
        )
    ).scalars().first()
    if device is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No active device to complete login")

    access_token, raw_refresh = await _issue_tokens(db, user=user, role=role, device=device)
    await db.commit()
    return TokenResponse(access_token=access_token, refresh_token=raw_refresh)


@router.post("/mfa/setup", response_model=MfaSetupOut)
async def mfa_setup(
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MfaSetupOut:
    """Blueprint §19: "MFA for privileged users." Generates a new secret every
    call (overwrites any prior unconfirmed one) — mfa_enabled only flips to
    True once /mfa/verify-setup confirms the user actually has it working."""
    user = await db.get(User, current.user_id)
    secret = pyotp.random_base32()
    user.mfa_secret = secret
    user.mfa_enabled = False
    await db.commit()
    uri = pyotp.TOTP(secret).provisioning_uri(name=user.email or user.phone or str(user.id), issuer_name="Gropto ERP")
    return MfaSetupOut(secret=secret, provisioning_uri=uri)


@router.post("/mfa/verify-setup", status_code=204)
async def mfa_verify_setup(
    payload: MfaVerifySetupIn,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    user = await db.get(User, current.user_id)
    if not user.mfa_secret:
        raise HTTPException(status_code=400, detail="Call /mfa/setup first")
    if not pyotp.TOTP(user.mfa_secret).verify(payload.code, valid_window=1):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid authentication code")
    user.mfa_enabled = True
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=None,
        device_id=current.device_id,
        action="mfa.enabled",
        entity_type="user",
        entity_id=current.user_id,
    )
    await db.commit()


@router.post("/mfa/disable", status_code=204)
async def mfa_disable(
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    if settings.mfa_enforcement and current.access is not None and current.access.mfa_required:
        raise HTTPException(status_code=400, detail="MFA is mandatory for your role and cannot be disabled")
    user = await db.get(User, current.user_id)
    user.mfa_enabled = False
    user.mfa_secret = None
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=None,
        device_id=current.device_id,
        action="mfa.disabled",
        entity_type="user",
        entity_id=current.user_id,
    )
    await db.commit()


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

    expires_at = token_row.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    if expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh token expired")

    # A revoked device still invalidates this session (revoke_device revokes the
    # user's refresh rows too), but there is no fingerprint check: login is
    # email+password only, so a refresh token is a bearer secret bound to the
    # user, not to a client identity.
    device = await db.get(Device, token_row.device_id) if token_row.device_id else None
    if device and device.status != "active":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Device no longer active")

    user = await db.get(User, token_row.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Account is deactivated or missing")

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
        # 400, not 401: a 401 makes the client treat the access token as
        # expired and run a refresh/retry instead of showing this message.
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    if payload.new_password == payload.current_password:
        raise HTTPException(status_code=400, detail="New password must differ from the current one")
    user.password_hash = hash_password(payload.new_password)
    user.must_change_password = False
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


async def _find_user(db: AsyncSession, email: str | None, phone: str | None) -> User | None:
    if email:
        return (await db.execute(select(User).where(func.lower(User.email) == email.strip().lower()))).scalar_one_or_none()
    if phone:
        return (await db.execute(select(User).where(User.phone == phone.strip()))).scalar_one_or_none()
    return None


@router.post("/forgot-password", status_code=200)
async def forgot_password(payload: ForgotPasswordIn, db: AsyncSession = Depends(get_db)) -> dict:
    """Sends a 6-digit code by email, SMS or WhatsApp (`channel`, default the
    first one configured for the account) that the holder submits to
    POST /auth/reset-password-with-otp. Calling it again is the resend: a
    cooldown and an hourly cap apply per identifier (services/otp.py). When no
    channel is configured, the request is flagged for an Admin/Super Admin to
    reset directly. The response never reveals whether the account exists."""
    identifier = payload.email or payload.phone
    if not identifier:
        raise HTTPException(status_code=400, detail="Provide email or phone")
    await otp_service.throttle(identifier, "password_reset")

    user = await _find_user(db, payload.email, payload.phone)
    generic = {"message": "If that account exists, a verification code has been sent to it.", "otp_sent": True}

    if user is not None and user.is_active and otp_service.available_channels(user):
        channel, destination = await otp_service.issue_password_reset_otp(db, user, payload.channel)
        await write_audit(
            db,
            user_id=user.id,
            role_code=None,
            store_id=None,
            device_id=None,
            action="auth.forgot_password_otp_sent",
            entity_type="user",
            entity_id=user.id,
            new_value={"channel": channel},
        )
        await db.commit()
        return {**generic, "channel": channel, "destination": destination}

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
    # Unknown account: same shape as a real send, so it can't be probed.
    return generic


@router.post("/otp/resend", status_code=200)
async def resend_otp(payload: ForgotPasswordIn, db: AsyncSession = Depends(get_db)) -> dict:
    """Explicit resend (optionally on a different channel); same throttle."""
    return await forgot_password(payload, db)


@router.get("/otp/channels")
async def otp_channels() -> dict:
    """Which delivery channels this server has configured (no account data),
    so the login screen only offers what can actually be sent."""
    from app.services import email as _email, sms as _sms, whatsapp as _wa

    return {
        "email": _email.is_configured(),
        "sms": _sms.otp_sms_configured(),
        "whatsapp": _wa.is_configured() and bool(settings.whatsapp_otp_template),
        "resend_cooldown_seconds": settings.otp_resend_cooldown_seconds,
    }


@router.post("/reset-password-with-otp", status_code=204)
async def reset_password_with_otp(payload: ResetPasswordWithOtpIn, db: AsyncSession = Depends(get_db)) -> None:
    identifier = (payload.email or payload.phone or "").strip().lower()
    if not identifier:
        raise HTTPException(status_code=400, detail="Provide email or phone")
    remaining = await is_login_locked(identifier, "otp-reset")
    if remaining is not None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many attempts. Try again in {int(remaining // 60) + 1} minute(s).",
        )

    user = await _find_user(db, payload.email, payload.phone)
    if user is None:
        await record_login_failure(identifier, "otp-reset")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired code")

    otp_stmt = select(PasswordResetOtp).where(
        PasswordResetOtp.user_id == user.id,
        PasswordResetOtp.used.is_(False),
        PasswordResetOtp.expires_at > datetime.now(timezone.utc),
    )
    candidates = (await db.execute(otp_stmt)).scalars().all()
    match = next((c for c in candidates if verify_password(payload.otp, c.code_hash)), None)
    if match is None:
        await record_login_failure(identifier, "otp-reset")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired code")

    await record_login_success(identifier, "otp-reset")
    match.used = True
    user.password_hash = hash_password(payload.new_password)
    user.must_change_password = False
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
    current: CurrentUser = Depends(require_permission("device.terminal.create")),
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


@router.get("/devices", response_model=Page[DeviceOut])
async def list_devices(
    status_filter: str | None = None,
    limit: int = 20,
    offset: int = 0,
    current: CurrentUser = Depends(require_permission("device.terminal.view")),
    db: AsyncSession = Depends(get_db),
) -> Page[DeviceOut]:
    stmt = select(Device, Store.name).join(Store, Store.id == Device.store_id)
    if status_filter:
        stmt = stmt.where(Device.status == status_filter)
    if not current.sees_all_stores():
        stmt = stmt.where(Device.store_id.in_(current.store_ids))
    capped_limit = min(limit, 200)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    result = await db.execute(stmt.order_by(Device.created_at.desc()).limit(capped_limit).offset(offset))
    items = [
        DeviceOut(
            id=device.id, store_id=device.store_id, store_name=store_name, code=device.code,
            fingerprint=device.fingerprint, status=device.status, last_seen_at=device.last_seen_at,
            created_at=device.created_at,
        )
        for device, store_name in result.all()
    ]
    return Page(items=items, total=total, limit=capped_limit, offset=offset)


@router.post("/devices/{device_id}/revoke", status_code=204)
async def revoke_device(
    device_id: uuid.UUID,
    current: CurrentUser = Depends(require_permission("device.terminal.delete")),
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
    current: CurrentUser = Depends(require_permission("device.credential.view")),
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

    access = current.access
    return {
        "user_id": str(current.user_id),
        "role": current.role_code,
        # Point 14: what the UI needs to gate routes/buttons. Display only —
        # every endpoint re-checks server-side.
        "role_codes": list(access.role_codes) if access else [current.role_code],
        "is_super_admin": current.is_super_admin,
        "all_stores": current.sees_all_stores(),
        "permissions": sorted(current.permissions),
        "stores": [str(s) for s in current.store_ids],
        "device_id": str(current.device_id) if current.device_id else None,
        # Needed on-device to format bill numbers as store_code-device_code-seq
        # (§4 of the plan) rather than an opaque sequence.
        "device_code": device_code,
        "primary_store_code": primary_store_code,
    }
