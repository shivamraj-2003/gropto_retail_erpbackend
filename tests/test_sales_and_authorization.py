"""Covers two of the plan's own acceptance criteria: negative authorization
(a role without the permission gets 403, not a silently-hidden button) and a
real sale round-tripping through the same POST /api/v1/sync/push path the
Electron app uses. The sale test writes one real, trivial-value row to
whatever database DATABASE_URL points at — same as any of the app's own
integration behaviour, and safe to re-run (a fresh UUID each run, deduplicated
server-side if ever replayed)."""

import uuid
from datetime import datetime, timezone

from httpx import AsyncClient

from tests.conftest import auth_headers


async def test_cashier_cannot_list_approvals(client: AsyncClient, cashier_token: str):
    # approval.decide is super_admin-only (see AppShell.tsx's own nav role
    # gates and the seeded role_permission_map) — a cashier token must be
    # refused by the server, not just have the button hidden client-side.
    resp = await client.get("/api/v1/approvals", headers=auth_headers(cashier_token))
    assert resp.status_code == 403


async def test_super_admin_can_list_approvals(client: AsyncClient, super_admin_token: str):
    resp = await client.get("/api/v1/approvals", headers=auth_headers(super_admin_token))
    assert resp.status_code == 200


async def test_cashier_cannot_manage_users(client: AsyncClient, cashier_token: str):
    # user.manage is admin/super_admin-only; a well-formed but arbitrary
    # store_id keeps this a pure authorization check rather than a 422
    # from a missing required query param.
    resp = await client.get(
        "/api/v1/hr/employees",
        headers=auth_headers(cashier_token),
        params={"store_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 403


async def test_cashier_can_bill_a_real_sale_end_to_end(client: AsyncClient, cashier_token: str):
    headers = auth_headers(cashier_token)

    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    store_id = me["stores"][0]
    device_id = me["device_id"]
    cashier_id = me["user_id"]
    assert store_id and device_id, "cashier fixture must be logged in with a store and a registered device"

    products = (await client.get("/api/v1/products", headers=headers, params={"active_only": True})).json()
    assert products, "no active products to bill — seed at least one product before running this test"
    product = products[0]

    sale_id = str(uuid.uuid4())
    payload = {
        "device_id": device_id,
        "sales": [
            {
                "id": sale_id,
                "store_id": store_id,
                "device_id": device_id,
                "bill_number": f"PYTEST-{sale_id[:8]}",
                "cashier_id": cashier_id,
                "items": [
                    {
                        "product_id": product["id"],
                        "product_name_snapshot": product["name"],
                        "tax_rate_snapshot": product["tax_rate"],
                        "quantity": 1,
                        "unit_price": product["selling_price"],
                        "line_discount": 0,
                    }
                ],
                "payments": [{"mode": "cash", "amount": product["selling_price"]}],
                "discount_total": 0,
                "loyalty_points_redeemed": 0,
                "client_idempotency_key": sale_id,
                "billed_at": datetime.now(timezone.utc).isoformat(),
            }
        ],
    }

    resp = await client.post("/api/v1/sync/push", json=payload, headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["results"]) == 1
    assert body["results"][0]["verdict"] == "applied"

    # Replaying the exact same idempotency key must be deduplicated, not
    # double-billed — this is the sync engine's core safety property (§5).
    resp2 = await client.post("/api/v1/sync/push", json=payload, headers=headers)
    assert resp2.json()["results"][0]["verdict"] == "duplicate"
