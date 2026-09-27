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


async def test_login_from_unregistered_device_is_rejected(client: AsyncClient):
    resp = await client.post(
        "/api/v1/auth/login",
        json={
            "email": "admin@gropto.local",
            "password": "ChangeMe!123",
            "device_fingerprint": "a-device-nobody-registered",
        },
    )
    assert resp.status_code == 403


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
