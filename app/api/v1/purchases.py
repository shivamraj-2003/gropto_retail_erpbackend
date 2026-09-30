
import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission, require_store_access
from app.core.database import get_db
from app.models.models import Purchase, PurchaseItem, Vendor
from app.schemas.schemas import PurchaseCreate, PurchaseOut, VendorCreate, VendorOut
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
