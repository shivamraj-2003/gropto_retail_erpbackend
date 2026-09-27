from httpx import AsyncClient


async def test_health_endpoint_is_unauthenticated(client: AsyncClient):
    resp = await client.get("/health")
    assert resp.status_code == 200


async def test_sync_health_probe_is_unauthenticated(client: AsyncClient):
    resp = await client.get("/api/v1/sync/health")
    assert resp.status_code == 200
