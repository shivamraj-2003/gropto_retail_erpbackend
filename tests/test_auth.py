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


async def test_refresh_rotates_the_token_and_issues_a_working_access_token(client: AsyncClient):
    # The silent-refresh contract the frontend relies on: exchanging a valid
    # refresh token returns a NEW pair (rotation), and the new access token is
    # immediately usable on a protected route — not just structurally present.
    login_resp = await client.post(
        "/api/v1/auth/login",
        json={"email": "admin@gropto.local", "password": "ChangeMe!123", "device_fingerprint": "pytest-rotation"},
    )
    assert login_resp.status_code == 200, login_resp.text
    old_access, old_refresh = login_resp.json()["access_token"], login_resp.json()["refresh_token"]

    refresh_resp = await client.post("/api/v1/auth/refresh", json={"refresh_token": old_refresh})
    assert refresh_resp.status_code == 200, refresh_resp.text
    new_access, new_refresh = refresh_resp.json()["access_token"], refresh_resp.json()["refresh_token"]

    assert new_access != old_access
    assert new_refresh != old_refresh

    me_resp = await client.get("/api/v1/auth/me", headers=auth_headers(new_access))
    assert me_resp.status_code == 200, me_resp.text


async def test_refresh_with_an_invalid_token_is_rejected(client: AsyncClient):
    resp = await client.post("/api/v1/auth/refresh", json={"refresh_token": "this-token-was-never-issued"})
    assert resp.status_code == 401


async def test_reusing_a_rotated_refresh_token_revokes_the_whole_family(client: AsyncClient):
    # This is the exact scenario the frontend's silent-refresh must treat as
    # "reauth_required": a spent (already-rotated) refresh token being
    # presented again — real reuse, or two concurrent refreshes racing without
    # the frontend's single-flight guard. The server detects it and revokes
    # every token in the family, so even the freshly-issued sibling token from
    # the legitimate first use stops working too.
    login_resp = await client.post(
        "/api/v1/auth/login",
        json={"email": "admin@gropto.local", "password": "ChangeMe!123", "device_fingerprint": "pytest-reuse"},
    )
    assert login_resp.status_code == 200, login_resp.text
    original_refresh = login_resp.json()["refresh_token"]

    first_use = await client.post("/api/v1/auth/refresh", json={"refresh_token": original_refresh})
    assert first_use.status_code == 200, first_use.text
    rotated_refresh = first_use.json()["refresh_token"]

    # Reuse: present the already-rotated (now-revoked) token again.
    reuse_attempt = await client.post("/api/v1/auth/refresh", json={"refresh_token": original_refresh})
    assert reuse_attempt.status_code == 401
    assert "reuse" in reuse_attempt.json()["detail"].lower()

    # The legitimate rotated token from the first, valid use is also now dead
    # — the whole family was revoked, not just the reused one.
    second_use_attempt = await client.post("/api/v1/auth/refresh", json={"refresh_token": rotated_refresh})
    assert second_use_attempt.status_code == 401
