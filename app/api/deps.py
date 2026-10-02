import uuid

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.permission_catalog import GLOBAL_SCOPE_ROLES
from app.core.security import decode_token
from app.models.models import Device, User
from app.services.rbac import Access, resolve_access

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")

# Point 14: kept for existing imports. The source of truth for "sees every
# store" is now roles.scope_level (seeded 'global' for exactly these roles by
# the a8b9c0d1e2f4 migration), resolved per request in services/rbac.py.
ENTERPRISE_WIDE_ROLES = GLOBAL_SCOPE_ROLES


class CurrentUser:
    def __init__(
        self,
        user_id: uuid.UUID,
        role_code: str,
        store_ids: list[uuid.UUID],
        device_id: uuid.UUID | None,
        access: Access | None = None,
    ):
        self.user_id = user_id
        self.role_code = role_code
        self.store_ids = store_ids
        self.device_id = device_id
        self.access = access

    @property
    def is_super_admin(self) -> bool:
        if self.access is not None:
            return self.access.is_super_admin
        return self.role_code == "super_admin"

    @property
    def permissions(self) -> frozenset[str]:
        return self.access.permissions if self.access is not None else frozenset()

    def has_permission(self, code: str) -> bool:
        return self.is_super_admin or code in self.permissions

    def sees_all_stores(self) -> bool:
        if self.access is not None:
            return self.access.global_scope
        return self.role_code in ENTERPRISE_WIDE_ROLES

    def owns_store(self, store_id: uuid.UUID) -> bool:
        return self.sees_all_stores() or store_id in self.store_ids

    def owns_warehouse(self, warehouse_id: uuid.UUID) -> bool:
        """Warehouse scope only narrows: a user with no warehouse scope rows
        keeps the access they had before Point 14 (WMS was unscoped)."""
        if self.is_super_admin or self.access is None or not self.access.warehouse_ids:
            return True
        return warehouse_id in self.access.warehouse_ids


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

    # Point 14: role, permissions and store scope come from the database (via
    # services/rbac.py's short cache), not from the token's role/stores claims
    # — so a role change, permission removal or scope change applies on the
    # next request instead of lingering until the token expires.
    access = await resolve_access(db, user)
    return CurrentUser(
        user_id=user_id,
        role_code=access.role_code,
        store_ids=list(access.store_ids),
        device_id=device_id,
        access=access,
    )


def require_permission(permission_code: str):
    """Server-side authorization: the device is assumed hostile, this is the only gate that matters."""

    async def checker(current: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if not current.has_permission(permission_code):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"Permission denied: {permission_code}")
        return current

    return checker


def require_any_permission(*permission_codes: str):
    """For endpoints that serve two actions (e.g. approve vs reject) — the
    handler then checks the specific one with current.has_permission()."""

    async def checker(current: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if not any(current.has_permission(code) for code in permission_codes):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail=f"Permission denied: one of {', '.join(permission_codes)}"
            )
        return current

    return checker


def require_store_access(store_id: uuid.UUID, current: CurrentUser) -> None:
    if not current.owns_store(store_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized for this store")


async def require_super_admin(current: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    """A small number of actions are Super-Admin-only regardless of what
    permission grants exist — reserved for things an Admin must never do
    even via the approval queue."""
    if not current.is_super_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Super Admin only")
    return current


async def require_admin_or_super(current: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    """Both can call the endpoint; submit_or_apply decides what happens next —
    Super Admin's call applies immediately, Admin's queues for approval. Used
    for store onboarding/edits: a franchise's Admin can propose a new store
    or edit an existing one, but Shivam (Super Admin) signs off on it."""
    if not current.is_super_admin and current.role_code != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin or Super Admin only")
    return current


def require_warehouse_access(warehouse_id: uuid.UUID, current: CurrentUser) -> None:
    if not current.owns_warehouse(warehouse_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized for this warehouse")
