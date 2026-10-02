"""Point 14: resolves a user's effective access — roles → permissions → scope —
from the database on every request (via a short-lived cache), so a role or
permission change takes effect without waiting for the user's access token to
expire. The JWT's `role`/`stores` claims are no longer trusted for
authorization; only its `sub` is.

The cache is per process. A write clears this process's copy immediately
and bumps a shared version number in Redis (app/core/kv.py); every other
process checks that version at most every VERSION_CHECK_SECONDS and drops its
copy when it moved. Without Redis, CACHE_TTL_SECONDS bounds staleness.
"""

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from fastapi import HTTPException, status

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.kv import get_kv, is_shared, key
from app.models.models import Permission, Role, RolePermission, Store, User, UserRole, UserScope, UserStore
from app.models.models_phase4 import Cluster

if TYPE_CHECKING:
    from app.api.deps import CurrentUser

CACHE_TTL_SECONDS = 15.0
VERSION_CHECK_SECONDS = 2.0
_VERSION_KEY = "rbac_version"
logger = logging.getLogger("gropto.rbac")

SCOPE_TYPES = ("company", "region", "cluster", "city", "warehouse", "department")


@dataclass(frozen=True)
class Access:
    role_code: str
    role_codes: tuple[str, ...]
    is_super_admin: bool
    permissions: frozenset[str]
    global_scope: bool
    store_ids: tuple[uuid.UUID, ...]
    warehouse_ids: tuple[uuid.UUID, ...] = field(default_factory=tuple)
    department_ids: tuple[uuid.UUID, ...] = field(default_factory=tuple)
    company_ids: tuple[uuid.UUID, ...] = field(default_factory=tuple)
    role_active: bool = True
    mfa_required: bool = False


_cache: dict[uuid.UUID, tuple[float, Access]] = {}
_seen_version: str | None = None
_version_checked_at = 0.0
_pending_bumps: set[asyncio.Task] = set()


async def _bump_shared_version() -> None:
    global _seen_version
    try:
        _seen_version = str(await get_kv().incr(key(_VERSION_KEY), 30 * 24 * 3600))
    except Exception as exc:  # noqa: BLE001 — never fail the write that triggered it
        logger.error("Could not publish RBAC cache invalidation: %s", exc)


def invalidate(user_id: uuid.UUID | None = None) -> None:
    """Drop cached access — for one user, or everyone (role/permission edits
    affect every holder of the role) — here and, via Redis, in every other
    API process. A per-user drop is published as a full drop elsewhere."""
    if user_id is None:
        _cache.clear()
    else:
        _cache.pop(user_id, None)
    if is_shared():
        try:
            task = asyncio.get_running_loop().create_task(_bump_shared_version())
            _pending_bumps.add(task)
            task.add_done_callback(_pending_bumps.discard)
        except RuntimeError:
            pass  # no running loop (scripts/tests): nothing else to notify


async def _sync_shared_version(now: float) -> None:
    global _seen_version, _version_checked_at
    if not is_shared() or now - _version_checked_at < VERSION_CHECK_SECONDS:
        return
    _version_checked_at = now
    current = await get_kv().get(key(_VERSION_KEY))
    if current != _seen_version:
        if _seen_version is not None:
            _cache.clear()
        _seen_version = current


async def resolve_access(db: AsyncSession, user: User) -> Access:
    now = time.monotonic()
    await _sync_shared_version(now)
    hit = _cache.get(user.id)
    if hit is not None and hit[0] > now:
        return hit[1]
    access = await _load(db, user)
    _cache[user.id] = (now + CACHE_TTL_SECONDS, access)
    return access


async def _load(db: AsyncSession, user: User) -> Access:
    primary = await db.get(Role, user.role_id)
    extra_roles = (
        await db.execute(
            select(Role).join(UserRole, UserRole.role_id == Role.id).where(UserRole.user_id == user.id)
        )
    ).scalars().all()
    active_roles = [r for r in [primary, *extra_roles] if r is not None and r.is_active]
    role_codes = tuple(dict.fromkeys(r.code for r in active_roles))

    is_super = bool(user.is_super_admin) or (primary is not None and primary.code == "super_admin" and primary.is_active)

    permissions: frozenset[str] = frozenset()
    if active_roles:
        rows = await db.execute(
            select(Permission.code)
            .join(RolePermission, RolePermission.permission_id == Permission.id)
            .where(RolePermission.role_id.in_([r.id for r in active_roles]))
        )
        permissions = frozenset(rows.scalars().all())

    scopes = (await db.execute(select(UserScope).where(UserScope.user_id == user.id))).scalars().all()
    by_type: dict[str, list[UserScope]] = {t: [] for t in SCOPE_TYPES}
    for s in scopes:
        by_type.setdefault(s.scope_type, []).append(s)
    company_ids = tuple(s.scope_id for s in by_type["company"] if s.scope_id)

    direct = set((await db.execute(select(UserStore.store_id).where(UserStore.user_id == user.id))).scalars().all())

    # Stores reached through broader scopes. A Regional Manager's cluster
    # (clusters.regional_manager_id) counts as an implicit cluster scope.
    cluster_ids = {s.scope_id for s in by_type["cluster"] if s.scope_id}
    cluster_ids |= set(
        (await db.execute(select(Cluster.id).where(Cluster.regional_manager_id == user.id))).scalars().all()
    )
    conditions = []
    if company_ids:
        conditions.append(Store.company_id.in_(company_ids))
    region_ids = [s.scope_id for s in by_type["region"] if s.scope_id]
    if region_ids:
        conditions.append(Store.region_id.in_(region_ids))
    if cluster_ids:
        conditions.append(Store.cluster_id.in_(cluster_ids))
    cities = [s.scope_value for s in by_type["city"] if s.scope_value]
    if cities:
        conditions.append(Store.city.in_(cities))
    if conditions:
        direct |= set((await db.execute(select(Store.id).where(or_(*conditions)))).scalars().all())

    # 'global' roles see every store — unless the user is pinned to specific
    # companies, which is how cross-company access is fenced off.
    role_global = any(r.scope_level == "global" for r in active_roles)
    global_scope = is_super or (role_global and not company_ids)

    return Access(
        role_code=primary.code if primary else "",
        role_codes=role_codes,
        is_super_admin=is_super,
        permissions=permissions,
        global_scope=global_scope,
        store_ids=tuple(direct),
        warehouse_ids=tuple(s.scope_id for s in by_type["warehouse"] if s.scope_id),
        department_ids=tuple(s.scope_id for s in by_type["department"] if s.scope_id),
        company_ids=company_ids,
        role_active=primary is not None and primary.is_active,
        mfa_required=is_super or any(r.mfa_required for r in active_roles),
    )


# ---------------------------------------------------------------------------
# Anti-escalation guards, shared by users.py and the RBAC admin API
# ---------------------------------------------------------------------------


async def role_permission_codes(db: AsyncSession, role_id: uuid.UUID) -> set[str]:
    rows = await db.execute(
        select(Permission.code)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .where(RolePermission.role_id == role_id)
    )
    return set(rows.scalars().all())


async def assert_can_grant_role(db: AsyncSession, current: "CurrentUser", role: Role | None, role_code: str) -> Role:
    """A user may only hand out a role that is active and grants nothing they
    don't hold themselves; only a Super Admin can hand out super_admin."""
    if role is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unknown role: {role_code}")
    if not role.is_active:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Role {role.code} is deactivated")
    if current.is_super_admin:
        return role
    if role.code == "super_admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only a Super Admin can assign Super Admin")
    excess = await role_permission_codes(db, role.id) - set(current.permissions)
    if excess:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Role {role.code} grants {len(excess)} permission(s) you do not hold yourself",
        )
    return role


async def assert_can_manage_user(db: AsyncSession, current: "CurrentUser", target: User) -> None:
    """Nobody but a Super Admin touches a Super Admin's account, and a scoped
    administrator only manages users who share one of their stores."""
    if current.is_super_admin:
        return
    if target.id == current.user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You cannot change your own access")
    target_role = await db.get(Role, target.role_id)
    if target.is_super_admin or (target_role is not None and target_role.code == "super_admin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only a Super Admin can manage a Super Admin")
    if not current.sees_all_stores():
        target_stores = set(
            (await db.execute(select(UserStore.store_id).where(UserStore.user_id == target.id))).scalars().all()
        )
        if not target_stores & set(current.store_ids):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User is outside your scope")
