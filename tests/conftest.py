"""Integration tests run against a real, separately-running `uvicorn` process
talking to the app's own configured DATABASE_URL — this fixture starts one
automatically for the test session. (An earlier version of this suite drove
the FastAPI app in-process via httpx's ASGITransport, but on Windows that
puts the test runner's asyncio event loop and the app's own asyncpg
connection pool in the same process with two different event loops fighting
over the same connections — a well-known unfixable-in-app-code class of
"Event loop is closed" / "attached to a different loop" errors. A real
subprocess with its own loop, talked to over a real socket, is both simpler
and closer to how the Electron app actually calls this API.)

Needs `alembic upgrade head` and `python -m scripts.seed` +
`python -m scripts.seed_test_users` already applied, same as any other
environment this app runs in. Every login also needs a device already
registered against the login user's store — `scripts/seed.py` creates
exactly one (fingerprint "seed-bootstrap-device", store "ST001") shared by
every seeded test user. If that device has never completed its one-time
activation, logins here will fail with 403 "Invalid activation code" —
activate it once via the app or `POST /auth/devices/register` first.
"""

import socket
import subprocess
import sys
import time

import httpx
import pytest
import pytest_asyncio

TEST_DEVICE_FINGERPRINT = "seed-bootstrap-device"

CREDENTIALS = {
    "super_admin": ("admin@gropto.local", "ChangeMe!123"),
    "admin": ("admin.test@gropto.local", "Test@123"),
    "store_manager": ("manager@gropto.local", "Test@123"),
    "cashier": ("cashier@gropto.local", "Test@123"),
}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def server_base_url():
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                if httpx.get(f"{base_url}/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if proc.poll() is not None:
                raise RuntimeError("uvicorn exited before becoming healthy — see its output above")
            time.sleep(0.3)
        else:
            raise RuntimeError(f"uvicorn on {base_url} never became healthy within 20s")
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest_asyncio.fixture(scope="session")
async def client(server_base_url: str):
    # Session-scoped deliberately: a fresh httpx.AsyncClient per test opens a
    # fresh TCP connection to localhost each time, and on Windows enough of
    # those in quick succession (~20+) occasionally stalls on socket/port
    # allocation rather than the app being slow — reusing one connection pool
    # for the whole run avoids that class of flake entirely.
    async with httpx.AsyncClient(base_url=server_base_url, timeout=30) as ac:
        yield ac


async def login_as(client: httpx.AsyncClient, role: str) -> str:
    """Returns an access token for the given seeded role, or raises with the
    server's own error detail — a failure here almost always means the
    fixture data described in this module's docstring isn't in place yet,
    not a real app bug."""
    email, password = CREDENTIALS[role]
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password, "device_fingerprint": TEST_DEVICE_FINGERPRINT},
    )
    assert resp.status_code == 200, (
        f"login as {role} failed: {resp.status_code} {resp.text} "
        f"(is the '{TEST_DEVICE_FINGERPRINT}' device activated for this store?)"
    )
    return resp.json()["access_token"]


@pytest_asyncio.fixture
async def super_admin_token(client: httpx.AsyncClient) -> str:
    return await login_as(client, "super_admin")


@pytest_asyncio.fixture
async def admin_token(client: httpx.AsyncClient) -> str:
    return await login_as(client, "admin")


@pytest_asyncio.fixture
async def manager_token(client: httpx.AsyncClient) -> str:
    return await login_as(client, "store_manager")


@pytest_asyncio.fixture
async def cashier_token(client: httpx.AsyncClient) -> str:
    return await login_as(client, "cashier")


def auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}
