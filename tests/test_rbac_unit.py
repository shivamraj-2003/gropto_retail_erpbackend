"""Point 14 RBAC — database-free tests of the authorization core: the
permission catalogue, that every API route is gated by a catalogue code, the
Super Admin override, scope checks, the approval matrix's approver rules and
the anti-escalation guards. None of these use the live-server fixture."""

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute

from app.api.deps import CurrentUser, require_any_permission, require_permission
from app.core.permission_catalog import (
    ALL_CODES,
    CATALOG_LEGACY,
    MODULE_LABELS,
    ROLE_BLUEPRINT,
    expand_patterns,
    is_transaction_edit,
)
from app.main import app
from app.services import approvals as approvals_service
from app.services import rbac
from app.services.rbac import Access

STORE_A, STORE_B = uuid.uuid4(), uuid.uuid4()
WH_A, WH_B = uuid.uuid4(), uuid.uuid4()

PUBLIC_OR_SELF = {
    "/health", "/api/v1/sync/health", "/api/v1/auth/login", "/api/v1/auth/refresh", "/api/v1/auth/logout",
    "/api/v1/auth/forgot-password", "/api/v1/auth/reset-password-with-otp", "/api/v1/auth/mfa/login-verify",
    "/api/v1/auth/me", "/api/v1/auth/change-password", "/api/v1/auth/mfa/setup", "/api/v1/auth/mfa/verify-setup",
    "/api/v1/auth/mfa/disable", "/api/v1/rbac/me",
}


def make_user(role="cashier", perms=(), *, super_admin=False, global_scope=False, stores=(STORE_A,), warehouses=(), role_codes=None):
    access = Access(
        role_code=role,
        role_codes=tuple(role_codes or (role,)),
        is_super_admin=super_admin,
        permissions=frozenset(perms),
        global_scope=global_scope or super_admin,
        store_ids=tuple(stores),
        warehouse_ids=tuple(warehouses),
    )
    return CurrentUser(user_id=uuid.uuid4(), role_code=role, store_ids=list(stores), device_id=None, access=access)


def _route_gates(dep, out):
    call = dep.call
    if call is not None and getattr(call, "__qualname__", "").endswith("checker") and call.__closure__:
        for cell in call.__closure__:
            value = cell.cell_contents
            if isinstance(value, str):
                out.add(value)
            elif isinstance(value, tuple):
                out.update(v for v in value if isinstance(v, str))
    if call is not None and getattr(call, "__name__", "") == "require_super_admin":
        out.add("@super_admin")
    for sub in dep.dependencies:
        _route_gates(sub, out)


# --- catalogue ---------------------------------------------------------------


def test_catalog_codes_are_module_feature_action_and_known_modules():
    assert len(ALL_CODES) == len(set(ALL_CODES)) > 200
    for code in ALL_CODES:
        module, feature, action = code.split(".")
        assert module in MODULE_LABELS and feature and action


def test_every_api_route_is_gated_by_a_catalog_permission():
    ungated = []
    for route in app.routes:
        if not isinstance(route, APIRoute) or route.path in PUBLIC_OR_SELF:
            continue
        gates: set[str] = set()
        _route_gates(route.dependant, gates)
        if not gates:
            ungated.append(f"{sorted(route.methods)} {route.path}")
        for gate in gates - {"@super_admin"}:
            assert gate in CATALOG_LEGACY, f"{route.path} uses non-catalogue code {gate}"
    assert ungated == []


def test_system_admin_blueprint_never_edits_transactions():
    granted = expand_patterns(ROLE_BLUEPRINT["system_admin"])
    assert granted and not [c for c in granted if is_transaction_edit(c)]


def test_all_nine_blueprint_roles_have_a_blueprint():
    for code in ("ceo", "coo", "finance_head", "purchase_head", "regional_manager", "store_manager", "cashier",
                 "inventory_user", "system_admin"):
        assert expand_patterns(ROLE_BLUEPRINT[code]), code


# --- permission + super admin -------------------------------------------------


async def test_require_permission_allows_holder_and_denies_others():
    checker = require_permission("inventory.stock.view")
    holder = make_user(perms={"inventory.stock.view"})
    assert await checker(current=holder) is holder
    with pytest.raises(HTTPException) as exc:
        await checker(current=make_user(perms={"inventory.stock.adjust"}))
    assert exc.value.status_code == 403


async def test_super_admin_passes_every_permission_without_grants():
    sa = make_user(role="super_admin", super_admin=True)
    for code in ALL_CODES:
        assert await require_permission(code)(current=sa) is sa


async def test_super_admin_flag_works_independent_of_role():
    flagged = make_user(role="cashier", super_admin=True)
    assert flagged.is_super_admin and flagged.has_permission("rbac.role.delete")


async def test_require_any_permission():
    checker = require_any_permission("approval.request.approve", "approval.request.reject")
    assert await checker(current=make_user(perms={"approval.request.reject"}))
    with pytest.raises(HTTPException):
        await checker(current=make_user(perms={"approval.request.view"}))


def test_view_without_update_is_view_only():
    viewer = make_user(perms={"inventory.stock.view"})
    assert viewer.has_permission("inventory.stock.view")
    assert not viewer.has_permission("inventory.stock.adjust")


# --- scope ---------------------------------------------------------------------


def test_store_scope_rejects_cross_store():
    manager = make_user(role="store_manager", stores=(STORE_A,))
    assert manager.owns_store(STORE_A) and not manager.owns_store(STORE_B)


def test_global_scope_and_company_pinned_global_role():
    ceo = make_user(role="ceo", global_scope=True, stores=())
    assert ceo.owns_store(STORE_B)
    pinned = make_user(role="ceo", global_scope=False, stores=(STORE_A,))  # company scope resolved to STORE_A
    assert pinned.owns_store(STORE_A) and not pinned.owns_store(STORE_B)


def test_warehouse_scope_narrows_only_when_assigned():
    unscoped = make_user(role="inventory_user")
    assert unscoped.owns_warehouse(WH_B)
    scoped = make_user(role="inventory_user", warehouses=(WH_A,))
    assert scoped.owns_warehouse(WH_A) and not scoped.owns_warehouse(WH_B)


# --- approval matrix ---------------------------------------------------------------


class _FakeDB:
    """Only what assert_can_decide touches: one scalar lookup for 'already decided'."""

    def __init__(self, already=None):
        self._already = already

    async def execute(self, _stmt):
        return SimpleNamespace(scalar_one_or_none=lambda: self._already)


def _request(requested_by, store_id=STORE_A):
    return SimpleNamespace(id=uuid.uuid4(), request_type="purchase_order_approval", requested_by=requested_by,
                           store_id=store_id, entity_id=None, new_value={})


def _rule(**kw):
    base = dict(name="High-value PO", approver_permission="approval.request.approve", approver_role_codes=None,
                maker_checker=True, levels=1)
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture
def rule(monkeypatch):
    holder = {"rule": None}

    async def fake_rule(_db, _type, _amount):
        return holder["rule"]

    async def fake_amount(_db, _req):
        return 75000.0

    monkeypatch.setattr(approvals_service, "matching_rule", fake_rule)
    monkeypatch.setattr(approvals_service, "request_amount", fake_amount)
    return holder


APPROVER_PERMS = {"approval.request.approve", "approval.request.reject"}


async def test_correct_approver_allowed(rule):
    rule["rule"] = _rule(approver_role_codes=["finance_head"])
    approver = make_user(role="finance_head", perms=APPROVER_PERMS, global_scope=True)
    await approvals_service.assert_can_decide(_FakeDB(), _request(uuid.uuid4()), approver, approve=True)


async def test_wrong_approver_role_rejected(rule):
    rule["rule"] = _rule(approver_role_codes=["finance_head", "purchase_head"])
    approver = make_user(role="coo", perms=APPROVER_PERMS, global_scope=True)
    with pytest.raises(HTTPException) as exc:
        await approvals_service.assert_can_decide(_FakeDB(), _request(uuid.uuid4()), approver, approve=True)
    assert exc.value.status_code == 403


async def test_maker_cannot_check_own_request(rule):
    rule["rule"] = _rule()
    maker = make_user(role="coo", perms=APPROVER_PERMS, global_scope=True)
    with pytest.raises(HTTPException) as exc:
        await approvals_service.assert_can_decide(_FakeDB(), _request(maker.user_id), maker, approve=True)
    assert "Maker-checker" in exc.value.detail


async def test_reject_needs_reject_permission(rule):
    rule["rule"] = _rule()
    approve_only = make_user(role="coo", perms={"approval.request.approve"}, global_scope=True)
    with pytest.raises(HTTPException):
        await approvals_service.assert_can_decide(_FakeDB(), _request(uuid.uuid4()), approve_only, approve=False)


async def test_approver_outside_store_scope_rejected(rule):
    rule["rule"] = _rule()
    approver = make_user(role="store_manager", perms=APPROVER_PERMS, stores=(STORE_B,))
    with pytest.raises(HTTPException) as exc:
        await approvals_service.assert_can_decide(_FakeDB(), _request(uuid.uuid4(), STORE_A), approver, approve=True)
    assert exc.value.detail == "Not authorized for this store"


async def test_same_approver_cannot_fill_two_levels(rule):
    rule["rule"] = _rule(levels=2)
    approver = make_user(role="finance_head", perms=APPROVER_PERMS, global_scope=True)
    with pytest.raises(HTTPException) as exc:
        await approvals_service.assert_can_decide(_FakeDB(already=uuid.uuid4()), _request(uuid.uuid4()), approver, approve=True)
    assert exc.value.status_code == 409


async def test_super_admin_never_blocked_by_matrix(rule):
    rule["rule"] = _rule(approver_role_codes=["finance_head"])
    sa = make_user(role="super_admin", super_admin=True)
    await approvals_service.assert_can_decide(_FakeDB(), _request(sa.user_id), sa, approve=True)


# --- anti-escalation ------------------------------------------------------------------


async def test_cannot_assign_role_with_more_access(monkeypatch):
    async def fake_codes(_db, _role_id):
        return {"user.user.view", "rbac.role.delete"}

    monkeypatch.setattr(rbac, "role_permission_codes", fake_codes)
    actor = make_user(role="system_admin", perms={"user.user.view", "rbac.user_role.assign"}, global_scope=True)
    role = SimpleNamespace(id=uuid.uuid4(), code="custom", is_active=True)
    with pytest.raises(HTTPException) as exc:
        await rbac.assert_can_grant_role(None, actor, role, "custom")
    assert exc.value.status_code == 403


async def test_only_super_admin_assigns_super_admin(monkeypatch):
    async def fake_codes(_db, _role_id):
        return set()

    monkeypatch.setattr(rbac, "role_permission_codes", fake_codes)
    role = SimpleNamespace(id=uuid.uuid4(), code="super_admin", is_active=True)
    with pytest.raises(HTTPException):
        await rbac.assert_can_grant_role(None, make_user(role="admin", perms=set(ALL_CODES), global_scope=True), role, "super_admin")
    sa = make_user(role="super_admin", super_admin=True)
    assert await rbac.assert_can_grant_role(None, sa, role, "super_admin") is role


async def test_inactive_role_cannot_be_assigned():
    role = SimpleNamespace(id=uuid.uuid4(), code="old_role", is_active=False)
    with pytest.raises(HTTPException) as exc:
        await rbac.assert_can_grant_role(None, make_user(super_admin=True), role, "old_role")
    assert exc.value.status_code == 400


def test_cache_invalidation_drops_entries():
    rbac._cache[uuid.uuid4()] = (1e18, None)  # type: ignore[assignment]
    rbac.invalidate()
    assert rbac._cache == {}
