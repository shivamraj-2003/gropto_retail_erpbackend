import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission
from app.core.database import get_db
from app.core.security import hash_password
from app.models.models import Role, User, UserStore
from app.schemas.schemas import ASSIGNABLE_ROLES, ResetPasswordIn, UserCreateIn, UserCreateResult, UserOut, UsersPage
from app.services.approvals import submit_or_apply

router = APIRouter(prefix="/users", tags=["users"])


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
    current: CurrentUser = Depends(require_permission("user.manage")),
    db: AsyncSession = Depends(get_db),
) -> UsersPage:
    stmt = select(User, Role.code).join(Role, Role.id == User.role_id)
    if current.role_code != "super_admin":
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
    current: CurrentUser = Depends(require_permission("user.manage")),
    db: AsyncSession = Depends(get_db),
) -> UserCreateResult:
    """Super Admin's call applies immediately (submit_or_apply's own rule);
    an Admin's call queues for Super Admin approval — same engine every
    other sensitive change in this app already goes through, nothing
    user-management-specific about the gate itself."""
    if payload.role_code not in ASSIGNABLE_ROLES:
        raise HTTPException(status_code=400, detail=f"role_code must be one of: {', '.join(sorted(ASSIGNABLE_ROLES))}")
    if not payload.email and not payload.phone:
        raise HTTPException(status_code=400, detail="email or phone required")

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


@router.post("/{user_id}/reset-password", status_code=204)
async def reset_password(
    user_id: uuid.UUID,
    payload: ResetPasswordIn,
    current: CurrentUser = Depends(require_permission("user.manage")),
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
    user.password_hash = hash_password(payload.new_password)
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
