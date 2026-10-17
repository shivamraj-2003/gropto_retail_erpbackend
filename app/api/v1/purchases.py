
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission, require_store_access
from app.core.database import get_db
from app.models.models import Product, Purchase, PurchaseItem, Store, User, Vendor
from app.schemas.schemas import PurchaseCreate, PurchaseOut, VendorCreate, VendorOut, VendorUpdateIn
from app.services.audit import write_audit
from app.services.purchase_bill import build_bill
from app.services.vendor_ids import check_pan_matches_gstin, clean_vendor_ids, gstin_state_code, pan_from_gstin
from app.services.inventory import apply_movement

router = APIRouter(tags=["vendors-purchases"])


@router.get("/vendors", response_model=list[VendorOut])
async def list_vendors(
    db: AsyncSession = Depends(get_db),
    # Read access is broader than write: anyone entering a purchase needs to see
    # the vendor list, but only vendor.manage holders can create/edit a vendor.
    _current: CurrentUser = Depends(require_permission("vendor.vendor.view")),
    include_inactive: bool = False,
) -> list[Vendor]:
    stmt = select(Vendor).order_by(Vendor.name)
    if not include_inactive:  # purchase screens only offer vendors that are in use
        stmt = stmt.where(Vendor.is_active.is_(True))
    result = await db.execute(stmt)
    return list(result.scalars().all())


_TEXT_FIELDS = (
    "name", "legal_name", "address_line1", "address_line2", "city", "state", "contact_person", "phone", "alt_phone", "email",
    "category", "service_area", "bank_account_name", "bank_account_number", "bank_name",
)


def _clean_vendor(values: dict, existing: Vendor | None = None) -> dict:
    """Tidy the text fields, check GSTIN / PAN / CIN / FSSAI / IFSC / pincode, and fill a blank PAN from the GSTIN."""
    out = dict(values)
    for key in _TEXT_FIELDS:
        if key in out and isinstance(out[key], str):
            out[key] = out[key].strip() or None
    if "name" in out and not out["name"]:
        raise HTTPException(status_code=400, detail="The vendor needs a name")
    try:
        out.update(clean_vendor_ids(out))
        gstin = out["gst_number"] if "gst_number" in out else (existing.gst_number if existing else None)
        pan = out["pan_number"] if "pan_number" in out else (existing.pan_number if existing else None)
        check_pan_matches_gstin(gstin, pan)
        if not pan and pan_from_gstin(gstin):
            out["pan_number"] = pan_from_gstin(gstin)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return out


@router.post("/vendors", response_model=VendorOut, status_code=201)
async def create_vendor(
    payload: VendorCreate,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("vendor.vendor.create")),
) -> Vendor:
    values = _clean_vendor(payload.model_dump())
    # Point 6 audit fix: no duplicate-vendor prevention existed at all.
    if values.get("gst_number"):
        existing = (await db.execute(select(Vendor).where(Vendor.gst_number == values["gst_number"]))).scalar_one_or_none()
        if existing is not None:
            raise HTTPException(status_code=409, detail=f"A vendor with GSTIN {values['gst_number']} already exists ({existing.name})")

    vendor = Vendor(**values)
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
        new_value=values,
    )
    await db.commit()
    await db.refresh(vendor)
    return vendor


@router.patch("/vendors/{vendor_id}", response_model=VendorOut)
async def update_vendor(
    vendor_id: uuid.UUID,
    payload: VendorUpdateIn,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("vendor.vendor.update")),
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
    changes = _clean_vendor(changes, vendor)

    if "gst_number" in changes and changes["gst_number"] and changes["gst_number"] != vendor.gst_number:
        existing = (await db.execute(select(Vendor).where(Vendor.gst_number == changes["gst_number"]))).scalar_one_or_none()
        if existing is not None:
            raise HTTPException(status_code=409, detail=f"A vendor with GSTIN {changes['gst_number']} already exists ({existing.name})")

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
    current: CurrentUser = Depends(require_permission("procurement.purchase.create")),
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
    current: CurrentUser = Depends(require_permission("procurement.purchase.view")),
) -> list[Purchase]:
    stmt = select(Purchase).order_by(Purchase.created_at.desc()).offset(offset).limit(limit)
    if store_id:
        require_store_access(store_id, current)
        stmt = stmt.where(Purchase.store_id == store_id)
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.get("/purchases/{purchase_id}/bill")
async def purchase_bill(
    purchase_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("procurement.purchase.view")),
) -> dict:
    """A recorded purchase laid out as the vendor's bill: who sold it (name, GSTIN, CIN, PAN, address), who bought it,
    each item with its HSN and GST, the tax split and the total. Costs are the vendor's rate before GST."""
    purchase = await db.get(Purchase, purchase_id)
    if purchase is None:
        raise HTTPException(status_code=404, detail="Purchase not found")
    require_store_access(purchase.store_id, current)
    vendor = await db.get(Vendor, purchase.vendor_id)
    store = await db.get(Store, purchase.store_id)
    buyer = await db.get(User, purchase.created_by) if purchase.created_by else None
    products = {
        p.id: p
        for p in (await db.execute(select(Product).where(Product.id.in_([i.product_id for i in purchase.items])))).scalars().all()
    }

    # Within the state the tax is CGST + SGST; between states it is IGST (judged by the two GSTINs when both are known).
    vendor_state = gstin_state_code(vendor.gst_number) if vendor else None
    store_state = gstin_state_code(store.gstin) if store else None
    inter_state = bool(vendor_state and store_state and vendor_state != store_state)

    raw = []
    for item in purchase.items:
        p = products.get(item.product_id)
        raw.append(
            {
                "product_id": str(item.product_id),
                "name": p.name if p else "(removed product)",
                "sku": p.sku if p else "",
                "hsn_code": p.hsn_code if p else None,
                "uom": p.uom if p else "EA",
                "quantity": float(item.quantity),
                "unit_cost": float(item.unit_cost),
                "tax_rate": float(p.tax_rate) if p else 0.0,
            }
        )
    bill = build_bill(raw, inter_state=inter_state)
    return {
        "id": str(purchase.id),
        "number": f"PUR-{str(purchase.id)[:8].upper()}",
        "invoice_number": purchase.invoice_number,
        "invoice_date": purchase.invoice_date.isoformat() if purchase.invoice_date else None,
        "recorded_at": purchase.created_at.isoformat() if purchase.created_at else None,
        "recorded_by": buyer.full_name if buyer else None,
        "inter_state": inter_state,
        "vendor": VendorOut.model_validate(vendor).model_dump(mode="json") if vendor else None,
        "store": {"id": str(store.id), "name": store.name, "city": store.city, "state": store.state, "gstin": store.gstin} if store else None,
        "lines": bill["lines"],
        "by_rate": bill["by_rate"],
        "totals": bill["totals"],
    }
