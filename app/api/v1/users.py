import uuid

from pydantic import BaseModel

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission
from app.core.database import get_db
from app.core.security import hash_password
from app.models.models import Role, User, UserStore
from app.schemas.schemas import (
    ResetPasswordIn,
    UserCreateIn,
    UserCreateResult,
    UserOut,
    UsersPage,
    UserUpdateIn,
    UserUpdateResult,
)
from app.services.approvals import submit_or_apply
from app.services.rbac import assert_can_grant_role, assert_can_manage_user

router = APIRouter(prefix="/users", tags=["users"])

# Floor-level roles tied to one physical location — same constraint Cashier/
# Store Manager already had, extended to the new Associate/Packer/Rider
# roles (Point 2 audit). Department Head and Online Operations are
# deliberately left uncapped — nothing about either implies a single store.
SINGLE_STORE_ROLES = ("cashier", "store_manager", "associate", "packer", "rider")


async def _to_user_out(db: AsyncSession, user: User, role_code: str) -> UserOut:
    store_ids = list((await db.execute(select(UserStore.store_id).where(UserStore.user_id == user.id))).scalars().all())
    return UserOut(
        id=user.id,
        email=user.email,
        phone=user.phone,
        full_name=user.full_name,
        role_code=role_code,
        is_active=user.is_active,
        store_ids=store_ids,
        created_at=user.created_at,
    )


@router.get("", response_model=UsersPage)
async def list_users(
    limit: int = 20,
    offset: int = 0,
    store_id: uuid.UUID | None = None,
    current: CurrentUser = Depends(require_permission("user.user.view")),
    db: AsyncSession = Depends(get_db),
) -> UsersPage:
    stmt = select(User, Role.code).join(Role, Role.id == User.role_id)
    if store_id is not None:
        # the people assigned to this one store
        if not current.owns_store(store_id):
            raise HTTPException(status_code=403, detail="No access to this store")
        stmt = stmt.join(UserStore, UserStore.user_id == User.id).where(UserStore.store_id == store_id).distinct()
    elif not current.sees_all_stores():
        stmt = stmt.join(UserStore, UserStore.user_id == User.id).where(UserStore.store_id.in_(current.store_ids)).distinct()

    capped_limit = min(limit, 500)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    paged_stmt = stmt.order_by(User.created_at.desc()).limit(capped_limit).offset(offset)
    result = await db.execute(paged_stmt)
    items = [await _to_user_out(db, user, role_code) for user, role_code in result.all()]
    return UsersPage(items=items, total=total, limit=capped_limit, offset=offset)


@router.post("", response_model=UserCreateResult, status_code=201)
async def create_user(
    payload: UserCreateIn,
    current: CurrentUser = Depends(require_permission("user.user.create")),
    db: AsyncSession = Depends(get_db),
) -> UserCreateResult:
    """Super Admin's call applies immediately (submit_or_apply's own rule);
    an Admin's call queues for Super Admin approval — same engine every
    other sensitive change in this app already goes through, nothing
    user-management-specific about the gate itself."""
    # Point 14: any active role (custom roles included) is assignable, but
    # never one granting more than the caller holds, and super_admin only by
    # a Super Admin. Store assignment is limited to the caller's own scope.
    role = (await db.execute(select(Role).where(Role.code == payload.role_code))).scalar_one_or_none()
    await assert_can_grant_role(db, current, role, payload.role_code)
    for store_id in payload.store_ids:
        if not current.owns_store(store_id):
            raise HTTPException(status_code=403, detail="Cannot assign a store outside your scope")
    if not payload.email and not payload.phone:
        raise HTTPException(status_code=400, detail="email or phone required")
    if payload.role_code in SINGLE_STORE_ROLES and len(payload.store_ids) != 1:
        raise HTTPException(
            status_code=400,
            detail=f"{payload.role_code.replace('_', ' ').title()} must be assigned to exactly one store — pick the store they work in",
        )

    if payload.email:
        existing = await db.execute(select(User).where(User.email == payload.email))
        if existing.scalar_one_or_none() is not None:
            raise HTTPException(status_code=409, detail="Email already in use")

    new_user_id = uuid.uuid4()
    new_value = {
        "user_id": str(new_user_id),
        "email": payload.email,
        "full_name": payload.full_name,
        "phone": payload.phone,
        "password_hash": hash_password(payload.password),
        "role_code": payload.role_code,
        "store_ids": [str(s) for s in payload.store_ids],
    }
    request = await submit_or_apply(
        db,
        current=current,
        request_type="user_create",
        entity_type="user",
        entity_id=new_user_id,
        old_value=None,
        new_value=new_value,
        reason=f"New {payload.role_code} account: {payload.email or payload.phone}",
        store_id=payload.store_ids[0] if payload.store_ids else None,
    )
    await db.commit()

    if request.status == "approved":
        return UserCreateResult(status="created", user_id=new_user_id)
    return UserCreateResult(status="pending_approval", request_id=request.id)


@router.patch("/{user_id}", response_model=UserUpdateResult)
async def update_user(
    user_id: uuid.UUID,
    payload: UserUpdateIn,
    current: CurrentUser = Depends(require_permission("user.user.update")),
    db: AsyncSession = Depends(get_db),
) -> UserUpdateResult:
    """Reassigning a Cashier/Store Manager to a different store, or changing
    anyone's role, both land here — same submit_or_apply gate as everything
    else: a Super Admin's call applies immediately, an Admin's queues."""
    if payload.role_code is None and payload.store_ids is None:
        raise HTTPException(status_code=400, detail="Provide role_code and/or store_ids to change")

    result = await db.execute(select(User, Role.code).join(Role, Role.id == User.role_id).where(User.id == user_id))
    row = result.first()
    if row is None:
        raise HTTPException(status_code=404, detail="User not found")
    user, current_role_code = row
    current_store_ids = list((await db.execute(select(UserStore.store_id).where(UserStore.user_id == user.id))).scalars().all())

    await assert_can_manage_user(db, current, user)
    effective_role = payload.role_code or current_role_code
    if payload.role_code is not None:
        role = (await db.execute(select(Role).where(Role.code == payload.role_code))).scalar_one_or_none()
        await assert_can_grant_role(db, current, role, payload.role_code)
    for store_id in payload.store_ids or []:
        if not current.owns_store(store_id):
            raise HTTPException(status_code=403, detail="Cannot assign a store outside your scope")
    if payload.store_ids is not None and effective_role in SINGLE_STORE_ROLES and len(payload.store_ids) != 1:
        raise HTTPException(status_code=400, detail=f"{effective_role} must be assigned to exactly one store")
    if payload.store_ids is None and effective_role in SINGLE_STORE_ROLES and not current_store_ids:
        raise HTTPException(status_code=400, detail=f"{effective_role} needs a store — pick one for this user")

    old_value: dict = {"role_code": current_role_code, "store_ids": [str(s) for s in current_store_ids]}
    new_value: dict = {}
    if payload.role_code is not None:
        new_value["role_code"] = payload.role_code
    if payload.store_ids is not None:
        new_value["store_ids"] = [str(s) for s in payload.store_ids]

    request = await submit_or_apply(
        db,
        current=current,
        request_type="user_permission_change",
        entity_type="user",
        entity_id=user_id,
        old_value=old_value,
        new_value=new_value,
        reason=f"Updated {user.full_name}",
        store_id=payload.store_ids[0] if payload.store_ids else None,
    )
    await db.commit()

    if request.status == "approved":
        return UserUpdateResult(status="updated")
    return UserUpdateResult(status="pending_approval", request_id=request.id)


class UserActiveIn(BaseModel):
    active: bool


@router.post("/{user_id}/active")
async def set_user_active(
    user_id: uuid.UUID,
    payload: UserActiveIn,
    current: CurrentUser = Depends(require_permission("user.user.update")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Switch a person off (they left) or back on. Their history stays; they just cannot sign in.
    Switching off also signs them out of every device."""
    from app.models.models import RefreshToken
    from app.services.audit import write_audit

    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    if user.id == current.user_id:
        raise HTTPException(status_code=400, detail="You cannot switch off your own account")
    await assert_can_manage_user(db, current, user)
    if not payload.active and user.is_super_admin:
        others = (
            await db.execute(select(func.count()).select_from(User).where(User.is_super_admin.is_(True), User.is_active.is_(True), User.id != user.id))
        ).scalar_one()
        if others == 0:
            raise HTTPException(status_code=409, detail="This is the last active Super Admin — it cannot be switched off")
    user.is_active = payload.active
    if not payload.active:
        for token in (await db.execute(select(RefreshToken).where(RefreshToken.user_id == user.id, RefreshToken.revoked.is_(False)))).scalars().all():
            token.revoked = True
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=None,
        device_id=current.device_id,
        action="user.activated" if payload.active else "user.deactivated",
        entity_type="user",
        entity_id=user.id,
    )
    await db.commit()
    return {"status": "active" if payload.active else "switched_off"}


@router.post("/{user_id}/reset-password", status_code=204)
async def reset_password(
    user_id: uuid.UUID,
    payload: ResetPasswordIn,
    current: CurrentUser = Depends(require_permission("user.password.update")),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Direct reset by an Admin/Super Admin — the practical "forgot password"
    resolution when there's no email/SMS delivery configured: the account
    holder calls their store's admin, the admin sets a new password here and
    relays it. No approval round-trip; user.manage is already the gate."""
    from app.services.audit import write_audit

    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    await assert_can_manage_user(db, current, user)
    user.password_hash = hash_password(payload.new_password)
    # The admin knows this password — the holder must replace it on first use.
    user.must_change_password = True
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=None,
        device_id=current.device_id,
        action="user.password_reset",
        entity_type="user",
        entity_id=user.id,
    )
    await db.commit()
