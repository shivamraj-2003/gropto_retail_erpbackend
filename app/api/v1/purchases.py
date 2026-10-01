
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission, require_store_access
from app.core.database import get_db
from app.models.models import Purchase, PurchaseItem, Vendor
from app.schemas.schemas import PurchaseCreate, PurchaseOut, VendorCreate, VendorOut, VendorUpdateIn
from app.services.audit import write_audit
from app.services.inventory import apply_movement

router = APIRouter(tags=["vendors-purchases"])


@router.get("/vendors", response_model=list[VendorOut])
async def list_vendors(
    db: AsyncSession = Depends(get_db),
    # Read access is broader than write: anyone entering a purchase needs to see
    # the vendor list, but only vendor.manage holders can create/edit a vendor.
    _current: CurrentUser = Depends(require_permission("purchase.manage")),
) -> list[Vendor]:
    result = await db.execute(select(Vendor).where(Vendor.is_active.is_(True)))
    return list(result.scalars().all())


@router.post("/vendors", response_model=VendorOut, status_code=201)
async def create_vendor(
    payload: VendorCreate,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("vendor.manage")),
) -> Vendor:
    # Point 6 audit fix: no duplicate-vendor prevention existed at all.
    if payload.gst_number:
        existing = (await db.execute(select(Vendor).where(Vendor.gst_number == payload.gst_number))).scalar_one_or_none()
        if existing is not None:
            raise HTTPException(status_code=409, detail=f"A vendor with GST number {payload.gst_number} already exists")

    vendor = Vendor(**payload.model_dump())
    db.add(vendor)
    await db.flush()
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=None,
        device_id=current.device_id,
        action="vendor.created",
        entity_type="vendor",
        entity_id=vendor.id,
        new_value=payload.model_dump(),
    )
    await db.commit()
    await db.refresh(vendor)
    return vendor


@router.patch("/vendors/{vendor_id}", response_model=VendorOut)
async def update_vendor(
    vendor_id: uuid.UUID,
    payload: VendorUpdateIn,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("vendor.manage")),
) -> Vendor:
    """Point 6 audit fix: vendors could previously be created but never
    edited or deactivated — this is the entire missing update/deactivate
    path, audited with before/after values."""
    vendor = await db.get(Vendor, vendor_id)
    if vendor is None:
        raise HTTPException(status_code=404, detail="Vendor not found")

    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=400, detail="Provide at least one field to change")

    if "gst_number" in changes and changes["gst_number"] and changes["gst_number"] != vendor.gst_number:
        existing = (await db.execute(select(Vendor).where(Vendor.gst_number == changes["gst_number"]))).scalar_one_or_none()
        if existing is not None:
            raise HTTPException(status_code=409, detail=f"A vendor with GST number {changes['gst_number']} already exists")

    # Point 6 audit fix edge case: deactivating a vendor with open POs used to
    # have no guard at all.
    if changes.get("is_active") is False:
        from app.models.models_phase2 import PurchaseOrder

        open_po_count = (
            await db.execute(
                select(PurchaseOrder).where(
                    PurchaseOrder.vendor_id == vendor_id,
                    PurchaseOrder.status.in_(["pending_approval", "approved", "partially_received"]),
                )
            )
        ).scalars().all()
        if open_po_count:
            raise HTTPException(
                status_code=409,
                detail=f"Cannot deactivate — vendor has {len(open_po_count)} open purchase order(s); close or cancel them first",
            )

    old_value = {field: getattr(vendor, field) for field in changes}
    for field, value in changes.items():
        setattr(vendor, field, value)

    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=None,
        device_id=current.device_id,
        action="vendor.status_changed" if "is_active" in changes else "vendor.updated",
        entity_type="vendor",
        entity_id=vendor.id,
        old_value={k: (str(v) if v is not None else None) for k, v in old_value.items()},
        new_value={k: (str(v) if v is not None else None) for k, v in changes.items()},
    )
    await db.commit()
    await db.refresh(vendor)
    return vendor


@router.post("/purchases", response_model=PurchaseOut, status_code=201)
async def create_purchase(
    payload: PurchaseCreate,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("purchase.manage")),
) -> Purchase:
    """P1 scope: direct purchase entry with immediate stock-in (online only — the
    back office has connectivity). PO/GRN/three-way-match workflow is Phase 2."""
    require_store_access(payload.store_id, current)
    total = sum(item.quantity * item.unit_cost for item in payload.items)
    purchase = Purchase(
        vendor_id=payload.vendor_id,
        store_id=payload.store_id,
        invoice_number=payload.invoice_number,
        invoice_date=payload.invoice_date,
        total_amount=total,
        created_by=current.user_id,
    )
    db.add(purchase)
    await db.flush()

    for item in payload.items:
        db.add(PurchaseItem(purchase_id=purchase.id, product_id=item.product_id, quantity=item.quantity, unit_cost=item.unit_cost))
        await apply_movement(
            db,
            product_id=item.product_id,
            store_id=payload.store_id,
            delta=item.quantity,
            reason_code="purchase_receipt",
            source_type="purchase_item",
            source_id=purchase.id,
            created_by=current.user_id,
            device_id=current.device_id,
        )

    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=payload.store_id,
        device_id=current.device_id,
        action="purchase.created",
        entity_type="purchase",
        entity_id=purchase.id,
        new_value={"total_amount": float(total), "vendor_id": str(payload.vendor_id)},
    )
    await db.commit()
    await db.refresh(purchase)
    return purchase


@router.get("/purchases", response_model=list[PurchaseOut])
async def list_purchases(
    store_id: uuid.UUID | None = None,
    limit: int = 50,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("purchase.manage")),
) -> list[Purchase]:
    stmt = select(Purchase).order_by(Purchase.created_at.desc()).offset(offset).limit(limit)
    if store_id:
        require_store_access(store_id, current)
        stmt = stmt.where(Purchase.store_id == store_id)
    result = await db.execute(stmt)
    return list(result.scalars().all())
