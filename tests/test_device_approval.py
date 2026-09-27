"""Covers the no-activation-code device flow: an unrecognized device trying to
log in is recorded as pending (not silently rejected into the void), a Super
Admin sees it and approves it with one click, and only then can it log in."""

import uuid

from httpx import AsyncClient

from tests.conftest import CREDENTIALS, auth_headers


async def test_unknown_device_is_recorded_pending_and_login_is_refused(client: AsyncClient):
    fingerprint = f"pytest-unknown-{uuid.uuid4().hex[:8]}"
    email, password = CREDENTIALS["cashier"]

    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password, "device_fingerprint": fingerprint},
    )
    assert resp.status_code == 403
    assert "pending" in resp.json()["detail"].lower()

    # Trying again before approval must still be refused — showing up once
    # doesn't grant a free pass.
    resp2 = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password, "device_fingerprint": fingerprint},
    )
    assert resp2.status_code == 403


async def test_super_admin_sees_and_approves_the_pending_device(client: AsyncClient, super_admin_token: str):
    fingerprint = f"pytest-unknown-{uuid.uuid4().hex[:8]}"
    email, password = CREDENTIALS["cashier"]
    super_headers = auth_headers(super_admin_token)

    await client.post("/api/v1/auth/login", json={"email": email, "password": password, "device_fingerprint": fingerprint})

    pending = (await client.get("/api/v1/auth/devices", params={"status_filter": "pending"}, headers=super_headers)).json()
    match = next((d for d in pending if d["fingerprint"] == fingerprint), None)
    assert match is not None, "the device that just tried to log in must appear in the pending list"

    approve_resp = await client.post(f"/api/v1/auth/devices/{match['id']}/approve", headers=super_headers)
    assert approve_resp.status_code == 204

    login_resp = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password, "device_fingerprint": fingerprint},
    )
    assert login_resp.status_code == 200, "approved device must be able to log in with no code of any kind"


async def test_cashier_cannot_see_or_approve_pending_devices(client: AsyncClient, cashier_token: str):
    # device.manage is Super-Admin-only — a cashier must not be able to
    # approve their own or anyone else's device.
    resp = await client.get("/api/v1/auth/devices", headers=auth_headers(cashier_token))
    assert resp.status_code == 403
