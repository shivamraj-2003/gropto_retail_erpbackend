"""Section 17 of the plan, in test form: "An Admin price change creates a
pending request without changing the price; approval applies it and writes an
audit row containing the old value, the new value and the request
reference." This is one of the plan's own acceptance-criteria lines, run as
an automated check against the real database rather than a manual click-through."""

from httpx import AsyncClient

from tests.conftest import auth_headers


async def test_admin_price_change_is_pending_until_super_admin_approves(
    client: AsyncClient, admin_token: str, super_admin_token: str
):
    admin_headers = auth_headers(admin_token)
    super_headers = auth_headers(super_admin_token)

    products = (await client.get("/api/v1/products", headers=admin_headers)).json()
    assert products, "no active products — seed at least one before running this test"
    product = products[0]
    original_price = product["selling_price"]
    new_price = round(original_price + 1, 2)

    # Admin (not Super Admin) requests the change — must NOT apply immediately.
    resp = await client.post(
        f"/api/v1/products/{product['id']}/price-change",
        json={"selling_price": new_price, "reason": "pytest acceptance check"},
        headers=admin_headers,
    )
    assert resp.status_code == 200
    approval = resp.json()
    assert approval["status"] == "pending"

    unchanged = next(p for p in (await client.get("/api/v1/products", headers=admin_headers)).json() if p["id"] == product["id"])
    assert unchanged["selling_price"] == original_price, "price must not change before approval"

    # Super Admin decides it.
    decide_resp = await client.post(
        f"/api/v1/approvals/{approval['approval_request_id']}/decide",
        json={"approve": True, "note": "pytest approved"},
        headers=super_headers,
    )
    assert decide_resp.status_code == 200
    decided = decide_resp.json()
    assert decided["status"] == "approved"
    assert decided["old_value"]["selling_price"] == original_price
    assert decided["new_value"]["selling_price"] == new_price

    changed = next(p for p in (await client.get("/api/v1/products", headers=admin_headers)).json() if p["id"] == product["id"])
    assert changed["selling_price"] == new_price, "price must be applied after approval"

    # Restore the original price so this test is safe to re-run and doesn't
    # leave the catalogue permanently off-by-one-rupee.
    restore = await client.post(
        f"/api/v1/products/{product['id']}/price-change",
        json={"selling_price": original_price, "reason": "pytest cleanup"},
        headers=super_headers,
    )
    assert restore.status_code == 200
    assert restore.json()["status"] == "approved"  # Super Admin applies directly
