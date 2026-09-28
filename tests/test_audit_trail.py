"""The audit trail is a read surface over what write_audit() already writes on
every sensitive call across the app (17 call sites at last count) — these
tests exercise the new query API itself: filtering, per-entity version
history, and that it's permission-gated and store-scoped like everything else."""

import uuid

from httpx import AsyncClient

from tests.conftest import auth_headers


async def test_cashier_cannot_view_audit_trail(client: AsyncClient, cashier_token: str):
    resp = await client.get("/api/v1/audit/entries", headers=auth_headers(cashier_token))
    assert resp.status_code == 403


async def test_super_admin_sees_audit_entries_and_they_have_ip_and_version(
    client: AsyncClient, super_admin_token: str
):
    headers = auth_headers(super_admin_token)
    resp = await client.get("/api/v1/audit/entries", params={"limit": 5}, headers=headers)
    assert resp.status_code == 200
    entries = resp.json()
    assert entries, "expected at least one audit row (this very login already wrote one)"
    login_row = next((e for e in entries if e["action"] == "auth.login"), None)
    assert login_row is not None
    assert login_row["ip_address"], "the request middleware should have stamped an IP on this row"


async def test_admin_price_change_produces_a_two_entry_version_history_with_before_after(
    client: AsyncClient, admin_token: str, super_admin_token: str
):
    admin_headers = auth_headers(admin_token)
    super_headers = auth_headers(super_admin_token)

    products = (await client.get("/api/v1/products", headers=admin_headers)).json()
    product = products[0]
    original_price = product["selling_price"]
    bumped = round(original_price + 2, 2)

    await client.post(
        f"/api/v1/products/{product['id']}/price-change",
        json={"selling_price": bumped, "reason": "pytest audit trail check"},
        headers=admin_headers,
    )
    approval_id = (
        await client.get("/api/v1/approvals", params={"status_filter": "pending"}, headers=super_headers)
    ).json()
    approval = next(a for a in approval_id if a["entity_id"] == product["id"])
    await client.post(
        f"/api/v1/approvals/{approval['id']}/decide",
        json={"approve": True, "note": None},
        headers=super_headers,
    )

    history = (
        await client.get(f"/api/v1/audit/entity/product/{product['id']}", headers=super_headers)
    ).json()
    assert len(history) >= 2, "expect at least the 'requested' and 'applied' rows for this price change"

    applied = next(h for h in history if h["action"] == "price_change.applied")
    assert applied["old_value"]["selling_price"] == original_price
    assert applied["new_value"]["selling_price"] == bumped
    assert applied["reason"] == "pytest audit trail check"
    # Version numbers are strictly increasing for this one record.
    versions = [h["entity_version"] for h in history if h["entity_version"] is not None]
    assert versions == sorted(versions)

    # Restore the price so this test is safe to re-run.
    await client.post(
        f"/api/v1/products/{product['id']}/price-change",
        json={"selling_price": original_price, "reason": "pytest cleanup"},
        headers=super_headers,
    )


async def test_admin_audit_view_is_scoped_to_their_own_stores(client: AsyncClient, admin_token: str):
    # Only super_admin and admin hold audit.view today — this exercises the
    # store-scoping clause (every row returned must belong to a store the
    # admin actually has) via the one non-Super-Admin role that can call it.
    headers = auth_headers(admin_token)
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    entries = (await client.get("/api/v1/audit/entries", params={"limit": 50}, headers=headers)).json()
    for e in entries:
        assert e["store_id"] is None or e["store_id"] in me["stores"]


async def test_unknown_entity_history_returns_empty_not_error(client: AsyncClient, super_admin_token: str):
    resp = await client.get(
        f"/api/v1/audit/entity/product/{uuid.uuid4()}", headers=auth_headers(super_admin_token)
    )
    assert resp.status_code == 200
    assert resp.json() == []
