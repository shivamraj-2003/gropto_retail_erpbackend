import uuid

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import decode_token
from app.models.models import Device, Permission, Role, RolePermission, User

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")

# Central enterprise scoping for the blueprint roles that see every store
# platform-wide. Regional/Cluster Manager is deliberately excluded — per
# blueprint §14 ("Regional/Cluster Manager: Assigned stores and operational
# exceptions") they're scoped like Admin/Store Manager, via the normal
# user_stores assignment, not a full bypass. Shared with auth.py's login
# device-binding check so the two can't drift out of sync (they did once:
# login() only exempted super_admin/admin, locking every other enterprise
# role out with "User not assigned to this store" since they carry zero
# store_ids by design).
ENTERPRISE_WIDE_ROLES = (
    "super_admin",
    "admin",
    "system_admin",
    "ceo",
    "coo",
    "finance_head",
    "purchase_head",
)


class CurrentUser:
    def __init__(self, user_id: uuid.UUID, role_code: str, store_ids: list[uuid.UUID], device_id: uuid.UUID | None):
        self.user_id = user_id
        self.role_code = role_code
        self.store_ids = store_ids
        self.device_id = device_id

    def sees_all_stores(self) -> bool:
        return self.role_code in ENTERPRISE_WIDE_ROLES

    def owns_store(self, store_id: uuid.UUID) -> bool:
        return self.sees_all_stores() or store_id in self.store_ids


async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> CurrentUser:
    try:
        payload = decode_token(token)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from exc
    if payload.get("type") != "access":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Wrong token type")

    user_id = uuid.UUID(payload["sub"])
    store_ids = [uuid.UUID(s) for s in payload.get("stores", [])]
    device_id = uuid.UUID(payload["device_id"]) if payload.get("device_id") else None

    # The claims above are only trusted as far as they are checked here. An
    # access token is self-contained, so without these lookups a user who is
    # deactivated, or a device that gets revoked, keeps working until their token
    # happens to expire — up to a full ACCESS_TOKEN_EXPIRE_MINUTES window. Both
    # are primary-key lookups, and get_db is request-scoped so this adds two
    # indexed reads to a request that already does more.
    #
    # 401 rather than 403: the token is no longer good and no amount of
    # refreshing will fix it, which is exactly the case the client's
    # 401 -> refresh -> sign-out path already handles, so a deactivated account
    # or revoked till bounces to the login screen instead of erroring on screen.
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Account no longer exists")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Account has been deactivated")

    if device_id is not None:
        device = await db.get(Device, device_id)
        if device is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Device no longer registered")
        if device.status != "active":
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Device has been revoked")

    return CurrentUser(
        user_id=user_id,
        role_code=payload["role"],
        store_ids=store_ids,
        device_id=device_id,
    )


def require_permission(permission_code: str):
    """Server-side authorization: the device is assumed hostile, this is the only gate that matters."""

    async def checker(
        current: CurrentUser = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
    ) -> CurrentUser:
        if current.role_code == "super_admin":
            return current
        stmt = (
            select(Permission.code)
            .join(RolePermission, RolePermission.permission_id == Permission.id)
            .join(Role, Role.id == RolePermission.role_id)
            .where(Role.code == current.role_code, Permission.code == permission_code)
        )
        result = await db.execute(stmt)
        if result.scalar_one_or_none() is None:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Permission denied")
        return current

    return checker


def require_store_access(store_id: uuid.UUID, current: CurrentUser) -> None:
    if not current.owns_store(store_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized for this store")


async def require_super_admin(current: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    """A small number of actions are Super-Admin-only regardless of what
    permission grants exist — reserved for things an Admin must never do
    even via the approval queue."""
    if current.role_code != "super_admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Super Admin only")
    return current


async def require_admin_or_super(current: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    """Both can call the endpoint; submit_or_apply decides what happens next —
    Super Admin's call applies immediately, Admin's queues for approval. Used
    for store onboarding/edits: a franchise's Admin can propose a new store
    or edit an existing one, but Shivam (Super Admin) signs off on it."""
    if current.role_code not in ("super_admin", "admin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin or Super Admin only")
    return current
