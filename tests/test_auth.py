from httpx import AsyncClient

from tests.conftest import TEST_DEVICE_FINGERPRINT, auth_headers


async def test_login_with_wrong_password_is_rejected(client: AsyncClient):
    resp = await client.post(
        "/api/v1/auth/login",
        json={
            "email": "admin@gropto.local",
            "password": "definitely-wrong",
            "device_fingerprint": TEST_DEVICE_FINGERPRINT,
        },
    )
    assert resp.status_code == 401


async def test_login_from_a_brand_new_device_succeeds_immediately(client: AsyncClient):
    # Login is email+password only — a device nobody pre-registered still
    # logs in on the first try, no pending/approval gate.
    resp = await client.post(
        "/api/v1/auth/login",
        json={
            "email": "admin@gropto.local",
            "password": "ChangeMe!123",
            "device_fingerprint": "a-device-nobody-registered",
        },
    )
    assert resp.status_code == 200
    assert "access_token" in resp.json()


async def test_successful_login_returns_tokens(client: AsyncClient, super_admin_token: str):
    # super_admin_token fixture already asserts a 200 with an access_token —
    # this test exists to fail loudly and specifically if that stops being true.
    assert super_admin_token


async def test_me_reflects_the_logged_in_user(client: AsyncClient, super_admin_token: str):
    resp = await client.get("/api/v1/auth/me", headers=auth_headers(super_admin_token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["role"] == "super_admin"
    assert body["user_id"]


async def test_protected_route_without_token_is_unauthorized(client: AsyncClient):
    resp = await client.get("/api/v1/auth/me")
    assert resp.status_code == 401


async def test_protected_route_with_garbage_token_is_unauthorized(client: AsyncClient):
    resp = await client.get("/api/v1/auth/me", headers=auth_headers("not-a-real-jwt"))
    assert resp.status_code == 401


async def test_login_without_a_device_fingerprint_still_succeeds(client: AsyncClient):
    # Email+password is the only credential — the fingerprint is optional and
    # just rebinds the client to a Device row, so omitting it must not fail.
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": "admin@gropto.local", "password": "ChangeMe!123"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["access_token"] and body["refresh_token"]


async def test_refresh_ignores_a_mismatched_fingerprint(client: AsyncClient):
    # A refresh token is a bearer secret bound to the user, not to a client
    # identity: refreshing from a different device (or with none at all) works.
    login_resp = await client.post(
        "/api/v1/auth/login",
        json={"email": "admin@gropto.local", "password": "ChangeMe!123", "device_fingerprint": "pytest-refresh-till-a"},
    )
    assert login_resp.status_code == 200, login_resp.text
    refresh_token = login_resp.json()["refresh_token"]

    resp = await client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": refresh_token, "device_fingerprint": "a-completely-different-till"},
    )
    assert resp.status_code == 200, resp.text
    assert "access_token" in resp.json()


async def test_refresh_without_a_device_fingerprint_still_succeeds(client: AsyncClient):
    login_resp = await client.post(
        "/api/v1/auth/login",
        json={"email": "admin@gropto.local", "password": "ChangeMe!123"},
    )
    assert login_resp.status_code == 200, login_resp.text

    resp = await client.post("/api/v1/auth/refresh", json={"refresh_token": login_resp.json()["refresh_token"]})
    assert resp.status_code == 200, resp.text
    assert "access_token" in resp.json()
