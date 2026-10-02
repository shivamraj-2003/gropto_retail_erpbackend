"""Point 14 — RBAC administration: roles, the permission catalogue, the
role-permission matrix, user role/scope assignment, the Super Admin flag, the
approval matrix and the RBAC audit trail.

Every write here is audited (action `rbac.*`) in the same transaction and
clears the access cache, so the change applies on the affected users' next
request. Non-Super-Admins can never grant more than they hold themselves,
never touch the super_admin role or flag, and never edit a role they hold.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, get_current_user, require_permission, require_super_admin
from app.core.database import get_db
from app.core.permission_catalog import MODULE_LABELS, is_transaction_edit
from app.models.models import (
    ApprovalRule,
    AuditLog,
    Permission,
    Role,
    RolePermission,
    Store,
    User,
    UserRole,
    UserScope,
    UserStore,
)
from app.schemas.schemas import Page
from app.schemas.schemas_rbac import (
    ApprovalRuleIn,
    ApprovalRuleOut,
    EffectiveAccessOut,
    MatrixOut,
    MatrixUpdateIn,
    PermissionOut,
    RbacAuditOut,
    RoleActivationIn,
    RoleCreateIn,
    RoleOut,
    RolePermissionsIn,
    RoleUpdateIn,
    RoleUserOut,
    ScopeEntry,
    SuperAdminIn,
    UserRolesIn,
    UserScopesIn,
)
from app.services import rbac
from app.services.approvals import _HANDLERS
from app.services.audit import write_audit

router = APIRouter(prefix="/rbac", tags=["rbac"])

# Same constraint users.py enforces at creation time.
SINGLE_STORE_ROLES = ("cashier", "store_manager", "associate", "packer", "rider")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def _audit(db: AsyncSession, current: CurrentUser, action: str, entity_type: str, entity_id, *, old=None, new=None, reason=None) -> None:
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=None,
        device_id=current.device_id,
        action=f"rbac.{action}",
        entity_type=entity_type,
        entity_id=entity_id,
        old_value=old,
        new_value=new,
        reason=reason,
    )


def _perm_out(p: Permission) -> PermissionOut:
    return PermissionOut(
        code=p.code,
        module=p.module or "",
        module_label=MODULE_LABELS.get(p.module or "", p.module or ""),
        feature=p.feature or "",
        action=p.action or "",
        description=p.description,
        transactional=is_transaction_edit(p.code),
    )


async def _catalog(db: AsyncSession) -> list[Permission]:
    rows = await db.execute(
        select(Permission)
        .where(Permission.is_deprecated.is_(False), Permission.module.is_not(None))
        .order_by(Permission.module, Permission.feature, Permission.action)
    )
    return list(rows.scalars().all())


async def _role_out(db: AsyncSession, role: Role) -> RoleOut:
    primary = await db.scalar(select(func.count()).select_from(User).where(User.role_id == role.id))
    extra = await db.scalar(select(func.count()).select_from(UserRole).where(UserRole.role_id == role.id))
    perms = await db.scalar(
        select(func.count())
        .select_from(RolePermission)
        .join(Permission, Permission.id == RolePermission.permission_id)
        .where(RolePermission.role_id == role.id, Permission.is_deprecated.is_(False))
    )
    out = RoleOut.model_validate(role)
    out.user_count = int(primary or 0) + int(extra or 0)
    out.permission_count = int(perms or 0)
    return out


async def _get_role(db: AsyncSession, role_id: uuid.UUID) -> Role:
    role = await db.get(Role, role_id)
    if role is None:
        raise HTTPException(status_code=404, detail="Role not found")
    return role


def _assert_can_edit_role(current: CurrentUser, role: Role) -> None:
    if current.is_super_admin:
        return
    if role.code == "super_admin":
        raise HTTPException(status_code=403, detail="Only a Super Admin can change the Super Admin role")
    held = set(current.access.role_codes) if current.access else {current.role_code}
    if role.code in held:
        raise HTTPException(status_code=403, detail="You cannot change a role you hold yourself")


async def _validate_grant_codes(
    db: AsyncSession, current: CurrentUser, role: Role, codes: set[str], *, changed: set[str] | None = None
) -> dict[str, uuid.UUID]:
    """`codes` is the role's full target set; `changed` (default: all of
    codes) is what this call adds or removes — the anti-escalation rule only
    applies to those, so editing a role that already holds something the
    actor lacks is fine as long as the actor doesn't touch it."""
    catalog = {p.code: p.id for p in await _catalog(db)}
    unknown = codes - set(catalog)
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown permission code(s): {', '.join(sorted(unknown))}")
    if role.code == "system_admin":
        forbidden = sorted(c for c in codes if is_transaction_edit(c))
        if forbidden:
            raise HTTPException(
                status_code=400,
                detail=f"System Admin may not edit transactions (blueprint §14); rejected: {', '.join(forbidden[:5])}",
            )
    if not current.is_super_admin:
        excess = (codes if changed is None else changed) - set(current.permissions)
        if excess:
            raise HTTPException(
                status_code=403, detail=f"You cannot grant or remove permissions you do not hold: {', '.join(sorted(excess)[:5])}"
            )
    return catalog


async def _role_codes(db: AsyncSession, role_id: uuid.UUID) -> set[str]:
    rows = await db.execute(
        select(Permission.code)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .where(RolePermission.role_id == role_id, Permission.is_deprecated.is_(False))
    )
    return set(rows.scalars().all())


async def _set_role_codes(db: AsyncSession, role: Role, target: set[str], catalog: dict[str, uuid.UUID]) -> tuple[set[str], set[str]]:
    """Replace the role's catalogue grants with `target`. Deprecated legacy
    grants are left untouched (nothing enforces them any more, and keeping
    them preserves history)."""
    existing = await _role_codes(db, role.id)
    added, removed = target - existing, existing - target
    if removed:
        await db.execute(
            delete(RolePermission).where(
                RolePermission.role_id == role.id,
                RolePermission.permission_id.in_([catalog[c] for c in removed]),
            )
        )
    for code in added:
        db.add(RolePermission(role_id=role.id, permission_id=catalog[code]))
    return added, removed


async def _access_out(db: AsyncSession, user: User) -> EffectiveAccessOut:
    access = await rbac.resolve_access(db, user)
    direct = list((await db.execute(select(UserStore.store_id).where(UserStore.user_id == user.id))).scalars().all())
    scopes = (await db.execute(select(UserScope).where(UserScope.user_id == user.id))).scalars().all()
    if access.is_super_admin:
        permissions = sorted(p.code for p in await _catalog(db))
    else:
        permissions = sorted(access.permissions)
    return EffectiveAccessOut(
        user_id=user.id,
        full_name=user.full_name,
        email=user.email,
        is_active=user.is_active,
        role=access.role_code,
        role_codes=list(access.role_codes),
        is_super_admin=access.is_super_admin,
        all_stores=access.global_scope,
        permissions=permissions,
        store_ids=list(access.store_ids),
        direct_store_ids=direct,
        scopes=[ScopeEntry(scope_type=s.scope_type, scope_id=s.scope_id, scope_value=s.scope_value) for s in scopes],
    )


# ---------------------------------------------------------------------------
# current user
# ---------------------------------------------------------------------------


@router.get("/me", response_model=EffectiveAccessOut)
async def my_access(current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> EffectiveAccessOut:
    """What the UI gates routes/buttons on. Display only — every endpoint
    re-checks server-side."""
    user = await db.get(User, current.user_id)
    return await _access_out(db, user)


# ---------------------------------------------------------------------------
# catalogue + roles
# ---------------------------------------------------------------------------


@router.get("/permissions", response_model=list[PermissionOut])
async def list_permissions(
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("rbac.permission.view")),
) -> list[PermissionOut]:
    return [_perm_out(p) for p in await _catalog(db)]


@router.get("/roles", response_model=list[RoleOut])
async def list_roles(
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("rbac.role.view")),
) -> list[RoleOut]:
    roles = (await db.execute(select(Role).order_by(Role.is_system.desc(), Role.name))).scalars().all()
    return [await _role_out(db, r) for r in roles]


@router.post("/roles", response_model=RoleOut, status_code=201)
async def create_role(
    payload: RoleCreateIn,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("rbac.role.create")),
) -> RoleOut:
    if payload.code == "super_admin" or (await db.execute(select(Role.id).where(Role.code == payload.code))).first():
        raise HTTPException(status_code=409, detail=f"Role code {payload.code} already exists")
    if payload.scope_level == "global" and not current.sees_all_stores():
        raise HTTPException(status_code=403, detail="Only an enterprise-wide user can create a company-wide role")
    codes = set(payload.permission_codes)
    if payload.clone_from_role_id is not None:
        source = await _get_role(db, payload.clone_from_role_id)
        if source.code == "super_admin":
            raise HTTPException(status_code=400, detail="Super Admin cannot be cloned; it has implicit full access")
        codes |= await _role_codes(db, source.id)
    role = Role(
        code=payload.code,
        name=payload.name,
        description=payload.description,
        scope_level=payload.scope_level,
        max_discount_percent=payload.max_discount_percent,
        max_discount_value=payload.max_discount_value,
        is_active=True,
        is_system=False,
    )
    db.add(role)
    await db.flush()
    catalog = await _validate_grant_codes(db, current, role, codes)
    await _set_role_codes(db, role, codes, catalog)
    await _audit(
        db, current, "role.created", "role", role.id,
        new={"code": role.code, "name": role.name, "scope_level": role.scope_level, "permissions": sorted(codes),
             "cloned_from": str(payload.clone_from_role_id) if payload.clone_from_role_id else None},
        reason=payload.reason,
    )
    await db.commit()
    rbac.invalidate()
    return await _role_out(db, role)


@router.patch("/roles/{role_id}", response_model=RoleOut)
async def update_role(
    role_id: uuid.UUID,
    payload: RoleUpdateIn,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("rbac.role.update")),
) -> RoleOut:
    role = await _get_role(db, role_id)
    _assert_can_edit_role(current, role)
    fields = payload.model_dump(exclude_unset=True, exclude={"reason"})
    if role.code == "super_admin" and fields.get("scope_level", "global") != "global":
        raise HTTPException(status_code=400, detail="Super Admin is always company-wide")
    if fields.get("scope_level") == "global" and not current.sees_all_stores():
        raise HTTPException(status_code=403, detail="Only an enterprise-wide user can make a role company-wide")
    old = {k: (float(v) if k.startswith("max_") else v) for k, v in ((k, getattr(role, k)) for k in fields)}
    for key, value in fields.items():
        setattr(role, key, value)
    role.updated_at = func.now()
    await _audit(db, current, "role.updated", "role", role.id, old=old, new=fields, reason=payload.reason)
    await db.commit()
    rbac.invalidate()
    await db.refresh(role)
    return await _role_out(db, role)


@router.post("/roles/{role_id}/activation", response_model=RoleOut)
async def set_role_activation(
    role_id: uuid.UUID,
    payload: RoleActivationIn,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("rbac.role.update")),
) -> RoleOut:
    role = await _get_role(db, role_id)
    _assert_can_edit_role(current, role)
    if role.code == "super_admin" and not payload.is_active:
        raise HTTPException(status_code=400, detail="The Super Admin role cannot be deactivated")
    old = role.is_active
    role.is_active = payload.is_active
    await _audit(
        db, current, "role.activated" if payload.is_active else "role.deactivated", "role", role.id,
        old={"is_active": old}, new={"is_active": payload.is_active}, reason=payload.reason,
    )
    await db.commit()
    rbac.invalidate()
    await db.refresh(role)
    return await _role_out(db, role)


@router.delete("/roles/{role_id}", status_code=204)
async def delete_role(
    role_id: uuid.UUID,
    reason: str | None = None,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("rbac.role.delete")),
) -> None:
    """Hard delete only for an unused custom role; blueprint roles and roles
    anyone still holds can only be deactivated."""
    role = await _get_role(db, role_id)
    _assert_can_edit_role(current, role)
    if role.is_system:
        raise HTTPException(status_code=409, detail="Built-in roles cannot be deleted — deactivate instead")
    out = await _role_out(db, role)
    if out.user_count:
        raise HTTPException(status_code=409, detail=f"Role is assigned to {out.user_count} user(s) — reassign them or deactivate the role")
    codes = await _role_codes(db, role.id)
    await db.execute(delete(RolePermission).where(RolePermission.role_id == role.id))
    await _audit(db, current, "role.deleted", "role", role.id, old={"code": role.code, "name": role.name, "permissions": sorted(codes)}, reason=reason)
    await db.delete(role)
    await db.commit()
    rbac.invalidate()


@router.get("/roles/{role_id}/permissions", response_model=list[str])
async def get_role_permissions(
    role_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("rbac.role.view")),
) -> list[str]:
    role = await _get_role(db, role_id)
    if role.code == "super_admin":
        return sorted(p.code for p in await _catalog(db))
    return sorted(await _role_codes(db, role.id))


@router.put("/roles/{role_id}/permissions", response_model=list[str])
async def set_role_permissions(
    role_id: uuid.UUID,
    payload: RolePermissionsIn,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("rbac.permission.assign")),
) -> list[str]:
    role = await _get_role(db, role_id)
    if role.code == "super_admin":
        raise HTTPException(status_code=400, detail="Super Admin holds every permission implicitly")
    _assert_can_edit_role(current, role)
    target = set(payload.permission_codes)
    existing = await _role_codes(db, role.id)
    catalog = await _validate_grant_codes(db, current, role, target, changed=target ^ existing)
    added, removed = await _set_role_codes(db, role, target, catalog)
    if added or removed:
        await _audit(
            db, current, "role.permissions_changed", "role", role.id,
            old={"removed": sorted(removed)}, new={"added": sorted(added)}, reason=payload.reason,
        )
    await db.commit()
    rbac.invalidate()
    return sorted(await _role_codes(db, role.id))


@router.get("/roles/{role_id}/users", response_model=list[RoleUserOut])
async def role_users(
    role_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("rbac.role.view")),
) -> list[RoleUserOut]:
    await _get_role(db, role_id)
    primary = (await db.execute(select(User).where(User.role_id == role_id))).scalars().all()
    extra = (
        await db.execute(select(User).join(UserRole, UserRole.user_id == User.id).where(UserRole.role_id == role_id))
    ).scalars().all()
    out = [RoleUserOut(user_id=u.id, full_name=u.full_name, email=u.email, is_active=u.is_active, assignment="primary") for u in primary]
    out += [RoleUserOut(user_id=u.id, full_name=u.full_name, email=u.email, is_active=u.is_active, assignment="additional") for u in extra]
    if not current.sees_all_stores():
        visible = set(
            (await db.execute(select(UserStore.user_id).where(UserStore.store_id.in_(current.store_ids)))).scalars().all()
        )
        out = [o for o in out if o.user_id in visible]
    return out


# ---------------------------------------------------------------------------
# matrix
# ---------------------------------------------------------------------------


@router.get("/matrix", response_model=MatrixOut)
async def get_matrix(
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("rbac.permission.view")),
) -> MatrixOut:
    catalog = await _catalog(db)
    roles = (await db.execute(select(Role).order_by(Role.is_system.desc(), Role.name))).scalars().all()
    rows = await db.execute(
        select(RolePermission.role_id, Permission.code)
        .join(Permission, Permission.id == RolePermission.permission_id)
        .where(Permission.is_deprecated.is_(False))
    )
    grants: dict[str, list[str]] = {str(r.id): [] for r in roles}
    for role_id, code in rows.all():
        grants.setdefault(str(role_id), []).append(code)
    for r in roles:
        if r.code == "super_admin":
            grants[str(r.id)] = [p.code for p in catalog]
    return MatrixOut(roles=[await _role_out(db, r) for r in roles], permissions=[_perm_out(p) for p in catalog], grants=grants)


@router.put("/matrix", response_model=MatrixOut)
async def update_matrix(
    payload: MatrixUpdateIn,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("rbac.permission.assign")),
) -> MatrixOut:
    by_role: dict[uuid.UUID, list] = {}
    for change in payload.changes:
        by_role.setdefault(change.role_id, []).append(change)
    for role_id, changes in by_role.items():
        role = await _get_role(db, role_id)
        if role.code == "super_admin":
            raise HTTPException(status_code=400, detail="Super Admin holds every permission implicitly")
        _assert_can_edit_role(current, role)
        existing = await _role_codes(db, role.id)
        target = set(existing)
        for c in changes:
            (target.add if c.granted else target.discard)(c.permission_code)
        catalog = await _validate_grant_codes(db, current, role, target, changed=target ^ existing)
        added, removed = await _set_role_codes(db, role, target, catalog)
        if added or removed:
            await _audit(
                db, current, "role.permissions_changed", "role", role.id,
                old={"removed": sorted(removed)}, new={"added": sorted(added)}, reason=payload.reason,
            )
    await db.commit()
    rbac.invalidate()
    return await get_matrix(db, current)


# ---------------------------------------------------------------------------
# users: roles, scopes, super admin
# ---------------------------------------------------------------------------


@router.get("/users", response_model=list[EffectiveAccessOut])
async def list_user_access(
    search: str | None = None,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("user.user.view")),
) -> list[EffectiveAccessOut]:
    stmt = select(User)
    if search:
        like = f"%{search.strip()}%"
        stmt = stmt.where(or_(User.full_name.ilike(like), User.email.ilike(like), User.phone.ilike(like)))
    if not current.sees_all_stores():
        stmt = stmt.join(UserStore, UserStore.user_id == User.id).where(UserStore.store_id.in_(current.store_ids)).distinct()
    users = (await db.execute(stmt.order_by(User.full_name).limit(min(limit, 200)))).scalars().all()
    return [await _access_out(db, u) for u in users]


async def _get_user_for_change(db: AsyncSession, current: CurrentUser, user_id: uuid.UUID) -> User:
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    await rbac.assert_can_manage_user(db, current, user)
    return user


@router.get("/users/{user_id}", response_model=EffectiveAccessOut)
async def user_access(
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("user.user.view")),
) -> EffectiveAccessOut:
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    if not current.sees_all_stores() and user.id != current.user_id:
        stores = set((await db.execute(select(UserStore.store_id).where(UserStore.user_id == user.id))).scalars().all())
        if not stores & set(current.store_ids):
            raise HTTPException(status_code=403, detail="User is outside your scope")
    return await _access_out(db, user)


@router.put("/users/{user_id}/roles", response_model=EffectiveAccessOut)
async def set_user_roles(
    user_id: uuid.UUID,
    payload: UserRolesIn,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("rbac.user_role.assign")),
) -> EffectiveAccessOut:
    user = await _get_user_for_change(db, current, user_id)
    codes = [payload.primary_role_code, *[c for c in payload.additional_role_codes if c != payload.primary_role_code]]
    roles: dict[str, Role] = {}
    for code in dict.fromkeys(codes):
        role = (await db.execute(select(Role).where(Role.code == code))).scalar_one_or_none()
        roles[code] = await rbac.assert_can_grant_role(db, current, role, code)
    primary = roles[payload.primary_role_code]
    if primary.code in SINGLE_STORE_ROLES:
        n = await db.scalar(select(func.count()).select_from(UserStore).where(UserStore.user_id == user.id))
        if (n or 0) > 1:
            raise HTTPException(status_code=400, detail=f"{primary.code} can only be assigned to one store — reduce the user's stores first")

    old_primary = await db.get(Role, user.role_id)
    old_extra = (
        await db.execute(select(Role.code).join(UserRole, UserRole.role_id == Role.id).where(UserRole.user_id == user.id))
    ).scalars().all()
    if not current.is_super_admin:
        # Removing a role is also a grant decision: only roles within the
        # actor's own authority may be taken away.
        for code in {old_primary.code if old_primary else "", *old_extra} - set(codes) - {""}:
            existing = (await db.execute(select(Role).where(Role.code == code))).scalar_one_or_none()
            if existing is not None:
                await rbac.assert_can_grant_role(db, current, existing, code)

    user.role_id = primary.id
    await db.execute(delete(UserRole).where(UserRole.user_id == user.id))
    for code in codes[1:]:
        db.add(UserRole(user_id=user.id, role_id=roles[code].id))
    await _audit(
        db, current, "user.roles_changed", "user", user.id,
        old={"primary": old_primary.code if old_primary else None, "additional": sorted(old_extra)},
        new={"primary": primary.code, "additional": sorted(codes[1:])},
        reason=payload.reason,
    )
    await db.commit()
    rbac.invalidate(user.id)
    await db.refresh(user)
    return await _access_out(db, user)


@router.put("/users/{user_id}/scopes", response_model=EffectiveAccessOut)
async def set_user_scopes(
    user_id: uuid.UUID,
    payload: UserScopesIn,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("rbac.scope.assign")),
) -> EffectiveAccessOut:
    user = await _get_user_for_change(db, current, user_id)
    role = await db.get(Role, user.role_id)
    store_ids = list(dict.fromkeys(payload.store_ids))
    if role is not None and role.code in SINGLE_STORE_ROLES and len(store_ids) > 1:
        raise HTTPException(status_code=400, detail=f"{role.code} can only be assigned to one store")
    for sid in store_ids:
        if await db.get(Store, sid) is None:
            raise HTTPException(status_code=400, detail=f"Unknown store {sid}")
        if not current.owns_store(sid):
            raise HTTPException(status_code=403, detail="Cannot assign a store outside your scope")
    for entry in payload.scopes:
        if entry.scope_type == "city":
            if not entry.scope_value:
                raise HTTPException(status_code=400, detail="City scope needs scope_value")
        elif entry.scope_id is None:
            raise HTTPException(status_code=400, detail=f"{entry.scope_type} scope needs scope_id")
        if entry.scope_type in ("company", "region", "cluster", "city") and not current.sees_all_stores():
            raise HTTPException(status_code=403, detail="Only an enterprise-wide user can assign company/region/cluster/city scope")
        if entry.scope_type == "warehouse" and not current.owns_warehouse(entry.scope_id):
            raise HTTPException(status_code=403, detail="Cannot assign a warehouse outside your scope")

    old_stores = list((await db.execute(select(UserStore.store_id).where(UserStore.user_id == user.id))).scalars().all())
    old_scopes = (await db.execute(select(UserScope).where(UserScope.user_id == user.id))).scalars().all()
    old = {
        "store_ids": sorted(str(s) for s in old_stores),
        "scopes": [{"type": s.scope_type, "id": str(s.scope_id) if s.scope_id else None, "value": s.scope_value} for s in old_scopes],
    }
    await db.execute(delete(UserStore).where(UserStore.user_id == user.id))
    await db.execute(delete(UserScope).where(UserScope.user_id == user.id))
    for sid in store_ids:
        db.add(UserStore(user_id=user.id, store_id=sid))
    seen: set[tuple] = set()
    for entry in payload.scopes:
        key = (entry.scope_type, entry.scope_id, entry.scope_value)
        if key in seen:
            continue
        seen.add(key)
        db.add(UserScope(user_id=user.id, scope_type=entry.scope_type, scope_id=entry.scope_id, scope_value=entry.scope_value))
    new = {
        "store_ids": sorted(str(s) for s in store_ids),
        "scopes": [{"type": t, "id": str(i) if i else None, "value": v} for t, i, v in seen],
    }
    await _audit(db, current, "user.scopes_changed", "user", user.id, old=old, new=new, reason=payload.reason)
    await db.commit()
    rbac.invalidate(user.id)
    return await _access_out(db, user)


@router.put("/users/{user_id}/super-admin", response_model=EffectiveAccessOut)
async def set_super_admin(
    user_id: uuid.UUID,
    payload: SuperAdminIn,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_super_admin),
) -> EffectiveAccessOut:
    """Only an existing Super Admin can grant or revoke Super Admin."""
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    if not payload.is_super_admin:
        role = await db.get(Role, user.role_id)
        if role is not None and role.code == "super_admin":
            raise HTTPException(status_code=400, detail="This user's primary role is Super Admin — change the role instead")
        remaining = await db.scalar(
            select(func.count())
            .select_from(User)
            .join(Role, Role.id == User.role_id)
            .where(User.is_active.is_(True), User.id != user.id, or_(User.is_super_admin.is_(True), Role.code == "super_admin"))
        )
        if not remaining:
            raise HTTPException(status_code=400, detail="At least one active Super Admin must remain")
    old = user.is_super_admin
    user.is_super_admin = payload.is_super_admin
    await _audit(
        db, current, "user.super_admin_granted" if payload.is_super_admin else "user.super_admin_revoked", "user", user.id,
        old={"is_super_admin": old}, new={"is_super_admin": payload.is_super_admin}, reason=payload.reason,
    )
    await db.commit()
    rbac.invalidate(user.id)
    return await _access_out(db, user)


# ---------------------------------------------------------------------------
# approval matrix
# ---------------------------------------------------------------------------


@router.get("/approval-request-types", response_model=list[str])
async def approval_request_types(_current: CurrentUser = Depends(require_permission("approval.rule.view"))) -> list[str]:
    return sorted(_HANDLERS)


@router.get("/approval-rules", response_model=list[ApprovalRuleOut])
async def list_approval_rules(
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("approval.rule.view")),
) -> list[ApprovalRule]:
    rows = await db.execute(select(ApprovalRule).order_by(ApprovalRule.request_type, ApprovalRule.min_amount.nulls_first()))
    return list(rows.scalars().all())


async def _validate_rule(db: AsyncSession, payload: ApprovalRuleIn) -> None:
    if payload.request_type not in _HANDLERS:
        raise HTTPException(status_code=400, detail=f"Unknown request_type {payload.request_type}")
    if payload.min_amount is not None and payload.max_amount is not None and payload.max_amount <= payload.min_amount:
        raise HTTPException(status_code=400, detail="max_amount must be greater than min_amount")
    if payload.approver_permission not in ("approval.request.approve",) and not (
        await db.execute(select(Permission.id).where(Permission.code == payload.approver_permission, Permission.is_deprecated.is_(False)))
    ).first():
        raise HTTPException(status_code=400, detail=f"Unknown permission {payload.approver_permission}")
    for code in payload.approver_role_codes or []:
        if not (await db.execute(select(Role.id).where(Role.code == code))).first():
            raise HTTPException(status_code=400, detail=f"Unknown role {code}")


def _rule_snapshot(rule: ApprovalRule) -> dict:
    return {
        "request_type": rule.request_type,
        "name": rule.name,
        "threshold_amount": float(rule.threshold_amount) if rule.threshold_amount is not None else None,
        "min_amount": float(rule.min_amount) if rule.min_amount is not None else None,
        "max_amount": float(rule.max_amount) if rule.max_amount is not None else None,
        "approver_permission": rule.approver_permission,
        "approver_role_codes": rule.approver_role_codes,
        "levels": rule.levels,
        "maker_checker": rule.maker_checker,
        "conditions": rule.conditions,
        "is_active": rule.is_active,
    }


@router.post("/approval-rules", response_model=ApprovalRuleOut, status_code=201)
async def create_approval_rule(
    payload: ApprovalRuleIn,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("approval.rule.configure")),
) -> ApprovalRule:
    await _validate_rule(db, payload)
    rule = ApprovalRule(**payload.model_dump(exclude={"reason"}))
    db.add(rule)
    await db.flush()
    await _audit(db, current, "approval_rule.created", "approval_rule", rule.id, new=_rule_snapshot(rule), reason=payload.reason)
    await db.commit()
    await db.refresh(rule)
    return rule


@router.put("/approval-rules/{rule_id}", response_model=ApprovalRuleOut)
async def update_approval_rule(
    rule_id: uuid.UUID,
    payload: ApprovalRuleIn,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("approval.rule.configure")),
) -> ApprovalRule:
    rule = await db.get(ApprovalRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="Approval rule not found")
    await _validate_rule(db, payload)
    old = _rule_snapshot(rule)
    for key, value in payload.model_dump(exclude={"reason"}).items():
        setattr(rule, key, value)
    rule.updated_at = func.now()
    await db.flush()
    await _audit(db, current, "approval_rule.updated", "approval_rule", rule.id, old=old, new=_rule_snapshot(rule), reason=payload.reason)
    await db.commit()
    await db.refresh(rule)
    return rule


# ---------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------


@router.get("/audit", response_model=Page[RbacAuditOut])
async def rbac_audit(
    limit: int = 50,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("rbac.audit.view")),
) -> Page[RbacAuditOut]:
    """RBAC/approval-matrix changes plus the user-management events that went
    through the approval engine. Read-only: no route updates or deletes
    audit_log rows."""
    stmt = select(AuditLog).where(
        or_(
            AuditLog.action.like("rbac.%"),
            AuditLog.action.like("user_create.%"),
            AuditLog.action.like("user_permission_change.%"),
            AuditLog.action == "user.password_reset",
        )
    )
    capped = min(limit, 200)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = await db.execute(stmt.order_by(AuditLog.created_at.desc()).limit(capped).offset(offset))
    return Page(items=list(rows.scalars().all()), total=total, limit=capped, offset=offset)
