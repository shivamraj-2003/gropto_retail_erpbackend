import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission, require_store_access
from app.core.database import get_db
from app.models.models import Device
from app.models.models_phase3 import DeviceConfig

router = APIRouter(prefix="/enterprise", tags=["enterprise"])


@router.get("/drilldown/store/{store_id}/skus")
async def store_sku_drilldown(
    store_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("dashboard.drilldown.view")),
) -> list[dict]:
    """Company -> store -> SKU drill-down: revenue and margin by product for one
    store, the level below the per-store dashboard in app/api/v1/dashboard.py."""
    require_store_access(store_id, current)
    rows = (
        await db.execute(
            text(
                """
                select p.sku, p.name, sum(si.quantity) qty, sum(si.line_total) revenue,
                       sum(si.line_total) - sum(si.quantity * p.purchase_price) est_margin
                from sale_items si
                join sales s on s.id = si.sale_id
                join products p on p.id = si.product_id
                where s.store_id = :store_id and s.status = 'completed'
                  and s.billed_at > now() - interval '30 days'
                group by p.sku, p.name
                order by revenue desc limit 50
                """
            ),
            {"store_id": str(store_id)},
        )
    ).all()
    return [
        {"sku": r.sku, "name": r.name, "qty_30d": float(r.qty), "revenue_30d": float(r.revenue), "est_margin_30d": float(r.est_margin)}
        for r in rows
    ]


@router.get("/fleet-health")
async def fleet_health(
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("device.health.view")),
) -> list[dict]:
    """Device health and fleet monitoring: status, last sync, and how many bills
    are still sitting in the failed queue for each device."""
    rows = (
        await db.execute(
            text(
                """
                select d.id, d.code, d.status, d.last_seen_at, d.created_at, st.name as store_name,
                       (select count(*) from sync_failures sf where sf.device_id = d.id and sf.resolved = false) as pending_failures
                from devices d
                join stores st on st.id = d.store_id
                order by d.last_seen_at desc nulls last
                """
            )
        )
    ).all()
    return [
        {
            "device_id": str(r.id),
            "code": r.code,
            "store_name": r.store_name,
            "status": r.status,
            "installed_at": r.created_at.isoformat(),
            "last_seen_at": r.last_seen_at.isoformat() if r.last_seen_at else None,
            "pending_sync_failures": int(r.pending_failures),
        }
        for r in rows
    ]


@router.get("/exceptions")
async def exception_feed(
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("dashboard.exception.view")),
) -> dict:
    """The CEO command-center exception feed: what's wrong right now, not a wall
    of reports — pending approvals, open fraud alerts, unresolved sync failures."""
    counts = (
        await db.execute(
            text(
                """
                select
                    (select count(*) from approval_requests where status = 'pending') as pending_approvals,
                    (select count(*) from fraud_alerts where status = 'open') as open_fraud_alerts,
                    (select count(*) from sync_failures where resolved = false) as unresolved_sync_failures,
                    (select count(*) from transfers where status = 'discrepancy') as transfer_discrepancies
                """
            )
        )
    ).one()
    return {
        "pending_approvals": int(counts.pending_approvals),
        "open_fraud_alerts": int(counts.open_fraud_alerts),
        "unresolved_sync_failures": int(counts.unresolved_sync_failures),
        "transfer_discrepancies": int(counts.transfer_discrepancies),
    }


@router.put("/devices/{device_id}/config")
async def push_device_config(
    device_id: uuid.UUID,
    config: dict,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("device.config.configure")),
) -> dict:
    """Blueprint §19 "centralized configuration pushed to every device" — the
    DeviceConfig table existed with no API surface at all before this. A
    device's sync worker reads its row via GET below on each poll."""
    device = await db.get(Device, device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found")
    require_store_access(device.store_id, current)
    existing = await db.get(DeviceConfig, device_id)
    if existing is None:
        existing = DeviceConfig(device_id=device_id, config=config)
        db.add(existing)
    else:
        existing.config = config
    await db.commit()
    return {"device_id": str(device_id), "config": config}


@router.get("/devices/{device_id}/config")
async def get_device_config(
    device_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("device.config.view")),
) -> dict:
    device = await db.get(Device, device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found")
    require_store_access(device.store_id, current)
    row = await db.get(DeviceConfig, device_id)
    return {"device_id": str(device_id), "config": row.config if row else {}}


@router.put("/config/broadcast")
async def broadcast_config(
    config: dict,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("device.config.configure")),
) -> dict:
    """Pushes the same config to every active device in one call — the
    fleet-wide half of centralized configuration, not just per-device."""
    device_ids = list((await db.execute(select(Device.id).where(Device.status == "active"))).scalars().all())
    for device_id in device_ids:
        existing = await db.get(DeviceConfig, device_id)
        if existing is None:
            db.add(DeviceConfig(device_id=device_id, config=config))
        else:
            existing.config = config
    await db.commit()
    return {"devices_updated": len(device_ids), "config": config}
