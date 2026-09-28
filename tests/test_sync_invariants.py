"""More lines from Section 17's acceptance criteria, each turned into a real
API call rather than a manual click-through:

- "The same batch replayed three times creates zero additional records"
- "Two devices selling the same SKU offline both sync with correct final
  stock" (stock falls by the sum, not overwritten)
"""

import uuid
from datetime import datetime, timezone

from httpx import AsyncClient

from tests.conftest import CREDENTIALS, TEST_DEVICE_FINGERPRINT, auth_headers


def _sale_payload(store_id: str, device_id: str, cashier_id: str, product: dict, qty: float = 1) -> dict:
    sale_id = str(uuid.uuid4())
    return {
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
                "quantity": qty,
                "unit_price": product["selling_price"],
                "line_discount": 0,
            }
        ],
        "payments": [{"mode": "cash", "amount": product["selling_price"] * qty}],
        "discount_total": 0,
        "loyalty_points_redeemed": 0,
        "client_idempotency_key": sale_id,
        "billed_at": datetime.now(timezone.utc).isoformat(),
    }


async def test_replaying_the_same_batch_three_times_creates_zero_extra_records(
    client: AsyncClient, cashier_token: str
):
    headers = auth_headers(cashier_token)
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    products = (await client.get("/api/v1/products", headers=headers)).json()
    product = products[0]

    sale = _sale_payload(me["stores"][0], me["device_id"], me["user_id"], product)
    payload = {"device_id": me["device_id"], "sales": [sale]}

    verdicts = []
    for _ in range(3):
        resp = await client.post("/api/v1/sync/push", json=payload, headers=headers)
        assert resp.status_code == 200
        verdicts.append(resp.json()["results"][0]["verdict"])

    assert verdicts[0] == "applied"
    assert verdicts[1] == "duplicate"
    assert verdicts[2] == "duplicate"


async def test_two_devices_selling_same_sku_offline_both_apply_stock_falls_by_sum(
    client: AsyncClient, super_admin_token: str, cashier_token: str
):
    super_headers = auth_headers(super_admin_token)
    cashier_headers = auth_headers(cashier_token)

    me = (await client.get("/api/v1/auth/me", headers=cashier_headers)).json()
    store_id = me["stores"][0]
    products = (await client.get("/api/v1/products", headers=cashier_headers)).json()
    product = products[0]

    # Register a second till for this store, mirroring the real device flow
    # (not a DB shortcut): a Super Admin registers it — active immediately,
    # no approval step — then the store's cashier logs in on it, exactly
    # what a brand-new physical till does.
    suffix = uuid.uuid4().hex[:8]
    second_fingerprint = f"pytest-second-till-{suffix}"
    register_resp = await client.post(
        "/api/v1/auth/devices/register",
        params={"store_id": store_id, "code": f"PYTEST-TILL2-{suffix}", "fingerprint": second_fingerprint},
        headers=super_headers,
    )
    assert register_resp.status_code == 201
    second_device_row_id = register_resp.json()["device_id"]

    cashier_email, cashier_password = CREDENTIALS["cashier"]
    activate_resp = await client.post(
        "/api/v1/auth/login",
        json={"email": cashier_email, "password": cashier_password, "device_fingerprint": second_fingerprint},
    )
    assert activate_resp.status_code == 200
    second_device_id = (
        await client.get("/api/v1/auth/me", headers=auth_headers(activate_resp.json()["access_token"]))
    ).json()["device_id"]
    assert second_device_id == second_device_row_id

    before = (
        await client.get(
            "/api/v1/inventory/balance",
            params={"product_id": product["id"], "store_id": store_id},
            headers=cashier_headers,
        )
    ).json()["quantity"]

    sale_a = _sale_payload(store_id, me["device_id"], me["user_id"], product, qty=1)
    sale_b = _sale_payload(store_id, second_device_id, me["user_id"], product, qty=1)

    resp_a = await client.post("/api/v1/sync/push", json={"device_id": me["device_id"], "sales": [sale_a]}, headers=cashier_headers)
    resp_b = await client.post("/api/v1/sync/push", json={"device_id": second_device_id, "sales": [sale_b]}, headers=cashier_headers)
    assert resp_a.json()["results"][0]["verdict"] == "applied"
    assert resp_b.json()["results"][0]["verdict"] == "applied"

    after = (
        await client.get(
            "/api/v1/inventory/balance",
            params={"product_id": product["id"], "store_id": store_id},
            headers=cashier_headers,
        )
    ).json()["quantity"]

    # Both sales are facts, not competing edits — stock falls by the sum of
    # both quantities sold, from two different devices, offline-style.
    assert after == before - 2
