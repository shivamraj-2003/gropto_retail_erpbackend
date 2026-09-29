"""Covers the email+password-only login flow: a device new to the system
logs in successfully on its very first attempt (no pending/approval gate),
is recorded and immediately active, and shows up in the device list for
visibility/revoke — but nothing blocks login itself."""

import uuid

from httpx import AsyncClient

from tests.conftest import CREDENTIALS, auth_headers


async def test_unknown_device_logs_in_immediately_no_approval_needed(client: AsyncClient):
    fingerprint = f"pytest-unknown-{uuid.uuid4().hex[:8]}"
    email, password = CREDENTIALS["cashier"]

    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password, "device_fingerprint": fingerprint},
    )
    assert resp.status_code == 200, "a new device must log in on its first attempt with just email+password"
    assert "access_token" in resp.json()


async def test_super_admin_sees_the_new_device_already_active(client: AsyncClient, super_admin_token: str):
    fingerprint = f"pytest-unknown-{uuid.uuid4().hex[:8]}"
    email, password = CREDENTIALS["cashier"]
    super_headers = auth_headers(super_admin_token)

    await client.post("/api/v1/auth/login", json={"email": email, "password": password, "device_fingerprint": fingerprint})

    devices = (await client.get("/api/v1/auth/devices", params={"status_filter": "active"}, headers=super_headers)).json()["items"]
    match = next((d for d in devices if d["fingerprint"] == fingerprint), None)
    assert match is not None, "the device that just logged in must appear in the device list, already active"
    assert match["status"] == "active"


async def test_revoked_device_is_refused_on_next_login(client: AsyncClient, super_admin_token: str):
    fingerprint = f"pytest-unknown-{uuid.uuid4().hex[:8]}"
    email, password = CREDENTIALS["cashier"]
    super_headers = auth_headers(super_admin_token)

    await client.post("/api/v1/auth/login", json={"email": email, "password": password, "device_fingerprint": fingerprint})
    devices = (await client.get("/api/v1/auth/devices", params={"status_filter": "active"}, headers=super_headers)).json()["items"]
    match = next(d for d in devices if d["fingerprint"] == fingerprint)

    revoke_resp = await client.post(f"/api/v1/auth/devices/{match['id']}/revoke", headers=super_headers)
    assert revoke_resp.status_code == 204

    login_resp = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password, "device_fingerprint": fingerprint},
    )
    assert login_resp.status_code == 403
    assert "revoked" in login_resp.json()["detail"].lower()


async def test_cashier_cannot_see_or_revoke_devices(client: AsyncClient, cashier_token: str):
    # device.manage is Super-Admin-only — a cashier must not be able to see
    # or revoke devices, even their own.
    resp = await client.get("/api/v1/auth/devices", headers=auth_headers(cashier_token))
    assert resp.status_code == 403


async def test_revoking_a_device_kills_its_live_access_token(client: AsyncClient, super_admin_token: str):
    """An access token is self-contained, so before deps.py checked the database
    a revoked till kept working until its token happened to expire. The check
    makes revocation effective immediately — which matters more now that
    ACCESS_TOKEN_EXPIRE_MINUTES is 60 rather than 15."""
    super_headers = auth_headers(super_admin_token)
    fingerprint = f"pytest-revoke-live-{uuid.uuid4().hex[:8]}"
    email, password = CREDENTIALS["cashier"]

    login_resp = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password, "device_fingerprint": fingerprint},
    )
    assert login_resp.status_code == 200, login_resp.text
    access_token = login_resp.json()["access_token"]
    headers = auth_headers(access_token)

    # The token works before the revoke.
    assert (await client.get("/api/v1/auth/me", headers=headers)).status_code == 200

    device_id = (await client.get("/api/v1/auth/me", headers=headers)).json()["device_id"]
    assert (await client.post(f"/api/v1/auth/devices/{device_id}/revoke", headers=super_headers)).status_code == 204

    # The very same still-unexpired token must now be rejected.
    after = await client.get("/api/v1/auth/me", headers=headers)
    assert after.status_code == 401, f"revoked device's token should be dead, got {after.status_code}"
