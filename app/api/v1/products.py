import io
import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from openpyxl import Workbook
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission, require_store_access
from app.core.database import get_db
from app.models.models import InventoryBalance, PriceVariantStock, Product, ProductBarcode, Store
from app.models.models_phase2 import Warehouse, WarehouseBalance
from app.schemas.schemas import ProductCreate, ProductOut, ProductPriceChangeRequest, ProductPullResponse, ProductUpdate
from app.services.approvals import submit_or_apply
from app.services.audit import write_audit
from app.services.catalog import barcode_owner, bump_revision, clean_barcodes
from app.services.price_stock import split_stock

router = APIRouter(prefix="/products", tags=["products"])


def _filtered(stmt, q: str | None, active_only: bool, missing: str | None):
    if active_only:
        stmt = stmt.where(Product.is_active.is_(True))
    if q and q.strip():
        like = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                Product.name.ilike(like),
                Product.sku.ilike(like),
                Product.barcode.ilike(like),
                Product.brand.ilike(like),
                Product.hsn_code.ilike(like),
                Product.id.in_(select(ProductBarcode.product_id).where(ProductBarcode.barcode.ilike(like))),
            )
        )
    # "Needs attention" views for a big catalogue: what is still incomplete.
    if missing == "hsn":
        stmt = stmt.where(or_(Product.hsn_code.is_(None), Product.hsn_code == ""))
    elif missing == "barcode":
        stmt = stmt.where(or_(Product.barcode.is_(None), Product.barcode == ""))
    elif missing == "price":
        stmt = stmt.where(or_(Product.selling_price <= 0, Product.mrp <= 0))
    elif missing == "cost":
        stmt = stmt.where(Product.purchase_price <= 0)
    return stmt


@router.get("", response_model=list[ProductOut])
async def list_products(
    q: str | None = None,
    active_only: bool = True,
    missing: str | None = None,
    limit: int = 200,
    offset: int = 0,
    store_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("catalog.product.view")),
) -> list[Product]:
    """`missing` = hsn | barcode | price | cost keeps only products still lacking that.
    With `store_id`, every product also carries `store_quantity`: how many that store holds right now."""
    stmt = _filtered(select(Product), q, active_only, missing)
    result = await db.execute(stmt.order_by(Product.name).limit(min(max(limit, 1), 500)).offset(max(offset, 0)))
    rows = list(result.scalars().all())
    if store_id is not None:
        require_store_access(store_id, current)
        held = {
            b.product_id: float(b.quantity)
            for b in (
                await db.execute(
                    select(InventoryBalance).where(InventoryBalance.store_id == store_id, InventoryBalance.product_id.in_([p.id for p in rows]))
                )
            ).scalars().all()
        } if rows else {}
        for p in rows:
            p.store_quantity = held.get(p.id, 0.0)  # type: ignore[attr-defined]
        with_variants = [p for p in rows if any(v.is_active for v in p.price_variants)]
        if with_variants:
            shares = {
                (s.variant_id): float(s.quantity)
                for s in (
                    await db.execute(
                        select(PriceVariantStock).where(
                            PriceVariantStock.store_id == store_id,
                            PriceVariantStock.variant_id.in_([v.id for p in with_variants for v in p.price_variants]),
                        )
                    )
                ).scalars().all()
            }
            for p in with_variants:
                p.price_split = _price_split(p, held.get(p.id, 0.0), shares)  # type: ignore[attr-defined]
    return rows


def _price_split(product: Product, total: float, shares: dict) -> list[dict]:
    """The units this store holds at each price of a product: the extra prices first, the current price with the rest."""
    active = [v for v in product.price_variants if v.is_active]
    current, per_variant = split_stock(total, [(str(v.id), shares.get(v.id, 0.0)) for v in active])
    out = [{"key": "", "label": "Current price", "mrp": float(product.mrp), "selling_price": float(product.selling_price), "quantity": current}]
    out += [{"key": str(v.id), "label": v.label, "mrp": float(v.mrp), "selling_price": float(v.selling_price), "quantity": per_variant[str(v.id)]} for v in active]
    return out


@router.get("/{product_id}/price-stock")
async def price_stock(
    product_id: uuid.UUID,
    store_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("catalog.product.view")),
) -> dict:
    """How many units a store holds at each price of one product."""
    require_store_access(store_id, current)
    product = await db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    balance = await db.get(InventoryBalance, (product_id, store_id))
    total = float(balance.quantity) if balance else 0.0
    shares = {
        s.variant_id: float(s.quantity)
        for s in (await db.execute(select(PriceVariantStock).where(PriceVariantStock.store_id == store_id))).scalars().all()
    }
    return {"product_id": str(product_id), "store_id": str(store_id), "total": total, "prices": _price_split(product, total, shares)}


class PriceStockIn(BaseModel):
    store_id: uuid.UUID
    quantity: float = Field(ge=0)


@router.put("/{product_id}/other-prices/{variant_id}/stock")
async def set_price_stock(
    product_id: uuid.UUID,
    variant_id: uuid.UUID,
    payload: PriceStockIn,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("inventory.stock.adjust")),
) -> dict:
    """Say how many of the store's units carry this extra price. It is a share of the stock the store already holds, so
    it cannot be more than that stock (together with the other extra prices); the rest is at the current price."""
    require_store_access(payload.store_id, current)
    product = await db.get(Product, product_id)
    variant = next((v for v in (product.price_variants if product else []) if v.id == variant_id and v.is_active), None)
    if product is None or variant is None:
        raise HTTPException(status_code=404, detail="That price was not found")
    balance = await db.get(InventoryBalance, (product_id, payload.store_id))
    total = float(balance.quantity) if balance else 0.0
    others = 0.0
    for v in product.price_variants:
        if v.is_active and v.id != variant_id:
            row = await db.get(PriceVariantStock, (v.id, payload.store_id))
            others += float(row.quantity) if row else 0.0
    if payload.quantity + others > total + 0.0005:
        room = max(total - others, 0.0)
        raise HTTPException(status_code=400, detail=f"The store holds {total:g} in all; at most {room:g} can be at this price")
    row = await db.get(PriceVariantStock, (variant_id, payload.store_id))
    old = float(row.quantity) if row else 0.0
    if row is None:
        db.add(PriceVariantStock(variant_id=variant_id, store_id=payload.store_id, quantity=payload.quantity))
    else:
        row.quantity = payload.quantity
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=payload.store_id,
        device_id=None,
        action="inventory.price_stock_set",
        entity_type="product",
        entity_id=product_id,
        old_value={"variant": variant.label, "quantity": old},
        new_value={"variant": variant.label, "quantity": payload.quantity},
    )
    await db.commit()
    return {"variant_id": str(variant_id), "quantity": payload.quantity}


@router.get("/count")
async def count_products(
    q: str | None = None,
    active_only: bool = True,
    missing: str | None = None,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("catalog.product.view")),
) -> dict:
    stmt = _filtered(select(func.count()).select_from(Product), q, active_only, missing)
    return {"total": (await db.execute(stmt)).scalar_one()}


@router.get("/export")
async def export_products(
    q: str | None = None,
    active_only: bool = True,
    missing: str | None = None,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("catalog.product.view")),
) -> StreamingResponse:
    """Every product (or the filtered ones) in the same columns the import reads, so the
    file can be edited in Excel and uploaded back through Import Data."""
    rows = (await db.execute(_filtered(select(Product), q, active_only, missing).order_by(Product.name))).scalars().all()
    wb = Workbook()
    ws = wb.active
    ws.title = "Products"
    ws.append(["sku", "name", "barcode", "uom", "purchase_price", "selling_price", "mrp", "tax_rate", "hsn_code", "other_barcodes"])
    for p in rows:
        ws.append(
            [
                p.sku, p.name, p.barcode or "", p.uom, float(p.purchase_price), float(p.selling_price), float(p.mrp),
                float(p.tax_rate), p.hsn_code or "", ", ".join(b.barcode for b in p.alt_barcodes),
            ]
        )
    for col, width in zip("ABCDEFGHIJ", (16, 36, 18, 8, 14, 14, 10, 8, 12, 30)):
        ws.column_dimensions[col].width = width
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="products.xlsx"'},
    )


@router.get("/pull", response_model=ProductPullResponse)
async def pull_catalogue(
    since_revision: int = 0,
    page_size: int = 1000,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("catalog.product.view")),
) -> ProductPullResponse:
    """Master-data pull against a revision watermark, for device catalogue bootstrap/sync."""
    stmt = select(Product).where(Product.revision > since_revision).order_by(Product.revision).limit(page_size)
    rows = list((await db.execute(stmt)).scalars().all())
    watermark = max((p.revision for p in rows), default=since_revision)
    return ProductPullResponse(products=rows, revision_watermark=watermark)


def _plain(value):
    return float(value) if hasattr(value, "quantize") else value


def _check_numbers(**values: float | None) -> None:
    labels = {"selling_price": "Selling price", "mrp": "MRP", "purchase_price": "Purchase price", "pack_size": "Pack size", "tax_rate": "GST %"}
    for key, value in values.items():
        if value is None:
            continue
        if value < 0:
            raise HTTPException(status_code=400, detail=f"{labels.get(key, key)} cannot be negative")
        if key == "tax_rate" and value > 100:
            raise HTTPException(status_code=400, detail="GST % must be between 0 and 100")


async def _assert_unique(
    db: AsyncSession, *, sku: str | None, barcodes: list[str], except_id: uuid.UUID | None = None
) -> None:
    if sku:
        clash = (await db.execute(select(Product.id).where(Product.sku == sku))).scalar_one_or_none()
        if clash is not None and clash != except_id:
            raise HTTPException(status_code=409, detail=f"SKU {sku} already exists")
    for code in barcodes:
        owner = await barcode_owner(db, code)
        if owner is not None and owner != except_id:
            raise HTTPException(status_code=409, detail=f"Barcode {code} already belongs to another product")


@router.post("", response_model=ProductOut, status_code=201)
async def create_product(
    payload: ProductCreate,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("catalog.product.create")),
) -> Product:
    data = payload.model_dump(exclude={"extra_barcodes"})
    data["sku"] = data["sku"].strip()
    data["name"] = data["name"].strip()
    data["barcode"], extras = clean_barcodes(data["barcode"], payload.extra_barcodes)
    if not data["sku"] or not data["name"]:
        raise HTTPException(status_code=400, detail="SKU and name are required")
    _check_numbers(selling_price=data["selling_price"], mrp=data["mrp"], purchase_price=data["purchase_price"], pack_size=data["pack_size"], tax_rate=data["tax_rate"])
    if not data["mrp"]:
        data["mrp"] = data["selling_price"]
    elif data["mrp"] < data["selling_price"]:
        raise HTTPException(status_code=400, detail="MRP cannot be lower than the selling price")
    await _assert_unique(db, sku=data["sku"], barcodes=([data["barcode"]] if data["barcode"] else []) + extras)
    product = Product(**data)
    product.alt_barcodes = [ProductBarcode(barcode=code) for code in extras]
    db.add(product)
    await db.flush()
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=None,
        device_id=current.device_id,
        action="product.created",
        entity_type="product",
        entity_id=product.id,
        new_value=payload.model_dump(mode="json"),
    )
    await db.commit()
    await db.refresh(product)
    return product


@router.put("/{product_id}", response_model=ProductOut)
async def update_product(
    product_id: uuid.UUID,
    payload: ProductUpdate,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("catalog.product.create")),
) -> Product:
    """Edit the details that are not customer-facing prices. Selling price and MRP
    change through /price-change (approval)."""
    product = await db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    changes = {k: v for k, v in payload.model_dump(exclude_unset=True, exclude={"extra_barcodes"}).items()}
    if "name" in changes:
        changes["name"] = (changes["name"] or "").strip()
        if not changes["name"]:
            raise HTTPException(status_code=400, detail="Name cannot be empty")
    for key in ("hsn_code", "brand", "uom"):
        if key in changes:
            changes[key] = (changes[key] or "").strip() or None
    # Barcodes: the final set (main + extras) is what must be unique.
    main_in = changes.get("barcode", product.barcode)
    extras_in = payload.extra_barcodes if payload.extra_barcodes is not None else [b.barcode for b in product.alt_barcodes]
    new_main, new_extras = clean_barcodes(main_in, extras_in)
    if "barcode" in changes:
        changes["barcode"] = new_main
    if changes.get("uom") is None and "uom" in changes:
        changes.pop("uom")
    _check_numbers(purchase_price=changes.get("purchase_price"), pack_size=changes.get("pack_size"), tax_rate=changes.get("tax_rate"))
    await _assert_unique(db, sku=None, barcodes=([new_main] if new_main else []) + new_extras, except_id=product_id)
    old = {k: _plain(getattr(product, k)) for k in changes}
    old_extras = [b.barcode for b in product.alt_barcodes]
    for key, value in changes.items():
        setattr(product, key, value)
    if payload.extra_barcodes is not None or "barcode" in changes:
        keep = set(new_extras)
        for existing in list(product.alt_barcodes):
            if existing.barcode not in keep:
                product.alt_barcodes.remove(existing)
        have = {b.barcode for b in product.alt_barcodes}
        await db.flush()  # free a code that moved from "extra" to "main" before the main column takes it
        for code in new_extras:
            if code not in have:
                product.alt_barcodes.append(ProductBarcode(barcode=code))
        if old_extras != new_extras:
            changes["extra_barcodes"] = new_extras
            old["extra_barcodes"] = old_extras
    await bump_revision(db, product)
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=None,
        device_id=current.device_id,
        action="product.updated",
        entity_type="product",
        entity_id=product.id,
        old_value=old,
        new_value=changes,
    )
    await db.commit()
    await db.refresh(product)
    return product


@router.post("/{product_id}/reactivate")
async def reactivate_product(
    product_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("catalog.product.create")),
) -> dict:
    product = await db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    product.is_active = True
    await bump_revision(db, product)
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=None,
        device_id=current.device_id,
        action="product.reactivated",
        entity_type="product",
        entity_id=product.id,
    )
    await db.commit()
    return {"status": "active"}


@router.get("/{product_id}/stock")
async def product_stock(
    product_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("catalog.product.view")),
) -> dict:
    """Where this product is: quantity in every store and warehouse the caller may see."""
    product = await db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    places: list[dict] = []
    store_rows = (
        await db.execute(
            select(Store.id, Store.name, Store.code, InventoryBalance.quantity, InventoryBalance.reserved, InventoryBalance.in_transit)
            .join(InventoryBalance, InventoryBalance.store_id == Store.id)
            .where(InventoryBalance.product_id == product_id)
        )
    ).all()
    for sid, name, code, qty, reserved, in_transit in store_rows:
        if current.owns_store(sid):
            places.append({"place_type": "store", "place_id": str(sid), "name": name, "code": code, "quantity": float(qty), "reserved": float(reserved), "on_the_way": float(in_transit)})
    wh_rows = (
        await db.execute(
            select(Warehouse.id, Warehouse.name, Warehouse.code, WarehouseBalance.quantity)
            .join(WarehouseBalance, WarehouseBalance.warehouse_id == Warehouse.id)
            .where(WarehouseBalance.product_id == product_id)
        )
    ).all()
    for wid, name, code, qty in wh_rows:
        if current.owns_warehouse(wid):
            places.append({"place_type": "warehouse", "place_id": str(wid), "name": name, "code": code, "quantity": float(qty), "reserved": 0.0, "on_the_way": 0.0})
    places.sort(key=lambda r: (-r["quantity"], r["name"]))
    return {"places": places, "total": round(sum(r["quantity"] for r in places), 3)}


@router.post("/{product_id}/price-change")
async def request_price_change(
    product_id: uuid.UUID,
    payload: ProductPriceChangeRequest,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("pricing.price.update")),
) -> dict:
    """Price changes are financially sensitive: routed through the approval engine.
    A Super Admin caller applies immediately; anyone else creates a pending request."""
    product = await db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")

    old_value = {"selling_price": float(product.selling_price), "mrp": float(product.mrp)}
    new_value = {k: v for k, v in payload.model_dump(exclude={"reason"}).items() if v is not None}

    request = await submit_or_apply(
        db,
        current=current,
        request_type="price_change",
        entity_type="product",
        entity_id=product_id,
        old_value=old_value,
        new_value=new_value,
        reason=payload.reason,
        store_id=None,
    )
    await db.commit()
    return {"approval_request_id": str(request.id), "status": request.status}


class PriceVariantRequest(BaseModel):
    mrp: float = Field(gt=0)
    selling_price: float = Field(gt=0)
    label: str = "Old stock"
    reason: str | None = None
    # How many units a store holds at this price (optional; can be set later too).
    store_id: uuid.UUID | None = None
    quantity: float = Field(default=0, ge=0)


@router.post("/{product_id}/other-prices")
async def add_other_price(
    product_id: uuid.UUID,
    payload: PriceVariantRequest,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("pricing.price.update")),
) -> dict:
    """One more price this product is on sale at (old stock after a price rise). Like any price change it goes through
    approval: a Super Admin applies it at once, anyone else creates a pending request."""
    product = await db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    if payload.selling_price > payload.mrp:
        raise HTTPException(status_code=400, detail="The selling price cannot be above the MRP")
    same = [
        v for v in product.price_variants
        if v.is_active and float(v.mrp) == payload.mrp and float(v.selling_price) == payload.selling_price
    ]
    if same or (float(product.mrp) == payload.mrp and float(product.selling_price) == payload.selling_price):
        raise HTTPException(status_code=409, detail="This product already has that price")
    request = await submit_or_apply(
        db,
        current=current,
        request_type="price_variant_add",
        entity_type="product",
        entity_id=product_id,
        old_value={"selling_price": float(product.selling_price), "mrp": float(product.mrp)},
        new_value={
            "mrp": payload.mrp,
            "selling_price": payload.selling_price,
            "label": payload.label.strip()[:40] or "Old stock",
            **({"store_id": str(payload.store_id), "quantity": payload.quantity} if payload.store_id and payload.quantity > 0 else {}),
        },
        reason=payload.reason,
        store_id=None,
    )
    await db.commit()
    return {"approval_request_id": str(request.id), "status": request.status}


@router.post("/{product_id}/other-prices/{variant_id}/remove")
async def remove_other_price(
    product_id: uuid.UUID,
    variant_id: uuid.UUID,
    reason: str | None = None,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("pricing.price.update")),
) -> dict:
    """Stop offering an extra price (the old stock is gone). Approval rules are the same as adding one."""
    product = await db.get(Product, product_id)
    variant = next((v for v in (product.price_variants if product else []) if v.id == variant_id and v.is_active), None)
    if product is None or variant is None:
        raise HTTPException(status_code=404, detail="That price was not found")
    request = await submit_or_apply(
        db,
        current=current,
        request_type="price_variant_remove",
        entity_type="product",
        entity_id=product_id,
        old_value={"mrp": float(variant.mrp), "selling_price": float(variant.selling_price)},
        new_value={"variant_id": str(variant_id)},
        reason=reason,
        store_id=None,
    )
    await db.commit()
    return {"approval_request_id": str(request.id), "status": request.status}


@router.post("/{product_id}/deactivate")
async def request_deactivation(
    product_id: uuid.UUID,
    reason: str | None = None,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("catalog.product.delete")),
) -> dict:
    product = await db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    request = await submit_or_apply(
        db,
        current=current,
        request_type="product_deactivation",
        entity_type="product",
        entity_id=product_id,
        old_value={"is_active": product.is_active},
        new_value={"is_active": False},
        reason=reason,
        store_id=None,
    )
    await db.commit()
    return {"approval_request_id": str(request.id), "status": request.status}
