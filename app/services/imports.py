"""Excel import never touches a business table directly. The file is parsed into
staging rows; the user sees a preview; only on confirmation does a single transaction
apply the valid rows, rolling back entirely on failure.

Three hard rules: no deletes via import ever (a missing row means nothing); price
changes via import route through the approval engine as one request holding every
delta; opening stock only imports into an empty balance.

Product sheets create new products and update existing ones by SKU. A blank cell
leaves that field alone. Name, barcode, unit, purchase price, GST and HSN apply
at once; selling price and MRP wait for approval (a Super Admin's apply at once).
"""

import uuid
from datetime import date, datetime

import openpyxl
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import CurrentUser
from app.models.models import ImportBatch, ImportStagingRow, InventoryBalance, Product, ProductBarcode
from app.models.models_phase2 import WarehouseBalance
from app.models.models_phase4 import InventoryBatch
from app.services.approvals import submit_or_apply
from app.services.audit import write_audit
from app.services.catalog import bump_revision
from app.services.inventory import adjust_warehouse_balance, apply_movement
from app.services.product_import_rules import (
    PRICE_FIELDS,
    clean_text,
    describe_changes,
    diff_product,
    header_key,
    parse_product_row,
)

PRODUCT_COLUMNS = ["sku", "name", "barcode", "uom", "purchase_price", "selling_price", "mrp", "tax_rate", "hsn_code"]
# batch_number, mfg_date and expiry_date are optional; when any is given the
# opening quantity is also recorded as an inventory batch so expiry tracking works.
# The location (store or warehouse) is picked on screen, not read from the sheet.
OPENING_STOCK_COLUMNS = ["sku", "quantity", "batch_number", "mfg_date", "expiry_date"]


def _parse_date(raw) -> date | None:
    """Excel gives real dates; typed cells arrive as text — accept ISO and DD-MM-YYYY / DD/MM/YYYY."""
    if raw is None or str(raw).strip() == "":
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    text_value = str(raw).strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text_value, fmt).date()
        except ValueError:
            continue
    raise ValueError(text_value)


async def get_batch_with_rows(db: AsyncSession, batch_id: uuid.UUID) -> ImportBatch | None:
    """`db.get()` + a later `batch.rows` access raises MissingGreenlet under async
    SQLAlchemy — the relationship's lazy="selectin" loader only fires as part of a
    query, not on a plain attribute access of an already-identity-mapped object.
    Load rows eagerly, in the same query, instead."""
    stmt = select(ImportBatch).where(ImportBatch.id == batch_id).options(selectinload(ImportBatch.rows))
    return (await db.execute(stmt)).scalar_one_or_none()


def _rows_of(file_bytes: bytes) -> tuple[list[str], list[tuple]]:
    wb = openpyxl.load_workbook(io_bytes(file_bytes), read_only=True)
    rows = list(wb.active.iter_rows(values_only=True))
    if not rows:
        raise ValueError("The file is empty")
    header = [header_key(h) for h in rows[0]]
    return header, [r for r in rows[1:] if any(c is not None and str(c).strip() != "" for c in r)]


def _product_snapshot(p: Product) -> dict:
    return {
        "other_barcodes": [b.barcode for b in p.alt_barcodes],
        "name": p.name, "barcode": p.barcode, "uom": p.uom, "hsn_code": p.hsn_code,
        "purchase_price": float(p.purchase_price or 0), "selling_price": float(p.selling_price or 0),
        "mrp": float(p.mrp or 0), "tax_rate": float(p.tax_rate or 0),
    }


async def stage_products_file(db: AsyncSession, *, file_bytes: bytes, filename: str, uploaded_by: uuid.UUID) -> ImportBatch:
    try:
        header, data_rows = _rows_of(file_bytes)
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001 — a corrupt/non-Excel upload should read as a plain message
        raise ValueError("Could not read this file — upload an .xlsx Excel file") from exc
    if "sku" not in header:
        raise ValueError("The first row must have a column called sku")

    batch = ImportBatch(import_type="products", uploaded_by=uploaded_by, file_name=filename, file_bytes=file_bytes, status="staged")
    db.add(batch)
    await db.flush()

    all_products = list((await db.execute(select(Product))).scalars().all())
    by_sku = {p.sku: p for p in all_products}
    barcode_owner = {p.barcode: p.sku for p in all_products if p.barcode}
    for p in all_products:
        for b in p.alt_barcodes:
            barcode_owner[b.barcode] = p.sku
    seen_skus: set[str] = set()
    seen_barcodes: dict[str, str] = {}

    for i, row in enumerate(data_rows, start=2):
        values = dict(zip(header, row, strict=False))
        sku = clean_text(values.get("sku")) or ""
        existing = by_sku.get(sku)
        provided, messages = parse_product_row(values, is_new=existing is None)
        if not sku:
            messages.append("SKU is missing")
        elif sku in seen_skus:
            messages.append("This SKU appears twice in the file")
        seen_skus.add(sku)

        for barcode in ([provided["barcode"]] if provided.get("barcode") else []) + provided.get("other_barcodes", []):
            owner = barcode_owner.get(barcode)
            if owner and owner != sku:
                messages.append(f"Barcode {barcode} already belongs to {owner}")
            elif barcode in seen_barcodes and seen_barcodes[barcode] != sku:
                messages.append(f"Barcode {barcode} is used by {seen_barcodes[barcode]} in this file")
            seen_barcodes.setdefault(barcode, sku)

        changes: dict = {}
        if messages:
            action = "error"
        elif existing is None:
            action = "create"
        else:
            changes = diff_product(_product_snapshot(existing), provided)
            action = "update" if changes else "skip"

        db.add(
            ImportStagingRow(
                batch_id=batch.id,
                line_number=i,
                parsed_values={"sku": sku, **provided, "_changes": changes},
                computed_action=action,
                validation_messages=messages,
            )
        )
    await db.commit()
    return batch


async def preview_batch(db: AsyncSession, batch_id: uuid.UUID) -> dict:
    batch = await get_batch_with_rows(db, batch_id)
    counts = {"create": 0, "update": 0, "error": 0, "skip": 0}
    for row in batch.rows:
        counts[row.computed_action or "error"] = counts.get(row.computed_action or "error", 0) + 1
    order = {"error": 0, "create": 1, "update": 2, "skip": 3}
    rows = []
    for row in sorted(batch.rows, key=lambda r: (order.get(r.computed_action or "error", 0), r.line_number)):
        v = row.parsed_values or {}
        detail = list(row.validation_messages or [])
        if row.computed_action == "update":
            detail = describe_changes(v.get("_changes") or {})
        elif row.computed_action == "skip":
            detail = ["Already up to date"]
        label = v.get("name") or v.get("sku")
        rows.append({"line": row.line_number, "sku": v.get("sku"), "label": label, "action": row.computed_action, "detail": detail})
    return {"batch_id": str(batch_id), "counts": counts, "total_rows": len(batch.rows), "rows": rows[:300]}


async def commit_products_batch(db: AsyncSession, batch_id: uuid.UUID, current: CurrentUser) -> dict:
    batch = await get_batch_with_rows(db, batch_id)
    if batch is None or batch.status != "staged":
        raise ValueError("Batch not found or already applied")

    created, updated = 0, 0
    price_changes: list[dict] = []
    by_sku = {p.sku: p for p in (await db.execute(select(Product))).scalars().all()}

    for row in batch.rows:
        if row.computed_action not in ("create", "update") or row.validation_messages:
            continue
        v = row.parsed_values
        product = by_sku.get(v["sku"])
        if row.computed_action == "create" and product is None:
            product = Product(
                sku=v["sku"], name=v["name"], barcode=v.get("barcode"), uom=v.get("uom") or "EA",
                selling_price=v["selling_price"], mrp=v.get("mrp") or v["selling_price"],
                tax_rate=v.get("tax_rate", 0), hsn_code=v.get("hsn_code"), purchase_price=v.get("purchase_price", 0),
            )
            product.alt_barcodes = [ProductBarcode(barcode=c) for c in v.get("other_barcodes", []) if c != product.barcode]
            db.add(product)
            by_sku[product.sku] = product
            created += 1
            continue
        if product is None:
            continue
        changes = v.get("_changes") or {}
        touched = False
        for field, change in changes.items():
            if field in PRICE_FIELDS:
                continue
            if field == "other_barcodes":
                have = {b.barcode for b in product.alt_barcodes}
                for code in change["new"]:
                    if code not in have and code != product.barcode:
                        product.alt_barcodes.append(ProductBarcode(barcode=code))
                touched = True
                continue
            setattr(product, field, change["new"])
            touched = True
        if touched:
            await bump_revision(db, product)
        price_part = {f: c for f, c in changes.items() if f in PRICE_FIELDS}
        if price_part:
            price_changes.append(
                {
                    "product_id": str(product.id),
                    "sku": product.sku,
                    "old": {f: c["old"] for f, c in price_part.items()},
                    "new": {f: c["new"] for f, c in price_part.items()},
                }
            )
        updated += 1

    pending_approval = 0
    if price_changes:
        request = await submit_or_apply(
            db,
            current=current,
            request_type="bulk_price_change",
            entity_type="import_batch",
            entity_id=batch.id,
            old_value=None,
            new_value={"changes": price_changes},
            reason=f"Price changes from import {batch.file_name}",
            store_id=None,
        )
        pending_approval = 0 if request.status == "approved" else len(price_changes)

    batch.status = "applied"
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=None,
        device_id=current.device_id,
        action="import.committed",
        entity_type="import_batch",
        entity_id=batch.id,
        new_value={"created": created, "updated": updated, "price_changes": len(price_changes), "pending_approval": pending_approval},
    )
    await db.commit()
    return {
        "created": created,
        "updated": updated,
        "price_changes": len(price_changes),
        "price_changes_pending_approval": pending_approval,
    }


async def stage_opening_stock_file(
    db: AsyncSession,
    *,
    file_bytes: bytes,
    filename: str,
    uploaded_by: uuid.UUID,
    store_id: uuid.UUID | None = None,
    warehouse_id: uuid.UUID | None = None,
) -> ImportBatch:
    """Opening stock for ONE location, picked on screen: a store or a warehouse."""
    if (store_id is None) == (warehouse_id is None):
        raise ValueError("Pick either a store or a warehouse to load stock into")
    try:
        header, data_rows = _rows_of(file_bytes)
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ValueError("Could not read this file — upload an .xlsx Excel file") from exc
    if "sku" not in header or "quantity" not in header:
        raise ValueError("The first row must have the columns sku and quantity")

    batch = ImportBatch(
        import_type="opening_stock", store_id=store_id, uploaded_by=uploaded_by, file_name=filename,
        file_bytes=file_bytes, status="staged",
    )
    db.add(batch)
    await db.flush()

    products = {p.sku: p for p in (await db.execute(select(Product))).scalars().all()}
    if store_id is not None:
        already = {
            b.product_id for b in (await db.execute(select(InventoryBalance).where(InventoryBalance.store_id == store_id))).scalars().all()
        }
    else:
        already = {
            b.product_id
            for b in (await db.execute(select(WarehouseBalance).where(WarehouseBalance.warehouse_id == warehouse_id))).scalars().all()
        }
    seen: set[str] = set()

    for i, row in enumerate(data_rows, start=2):
        values = dict(zip(header, row, strict=False))
        messages: list[str] = []
        sku = clean_text(values.get("sku")) or ""
        product = products.get(sku)
        if not sku:
            messages.append("SKU is missing")
        elif product is None:
            messages.append(f"Unknown SKU {sku} — add the product first")
        elif product.id in already:
            messages.append("Opening stock was already loaded for this product here")
        if sku in seen:
            messages.append("This SKU appears twice in the file")
        seen.add(sku)
        try:
            quantity = float(values.get("quantity") or 0)
            if quantity <= 0:
                messages.append("quantity must be more than 0")
        except (TypeError, ValueError):
            messages.append("quantity is not a number")
            quantity = 0

        batch_number = clean_text(values.get("batch_number"))
        mfg_date = expiry_date = None
        for field in ("mfg_date", "expiry_date"):
            try:
                parsed = _parse_date(values.get(field))
            except ValueError as exc:
                messages.append(f"{field} '{exc}' is not a valid date (use YYYY-MM-DD or DD-MM-YYYY)")
                continue
            if field == "mfg_date":
                mfg_date = parsed
            else:
                expiry_date = parsed
        if mfg_date and expiry_date and expiry_date <= mfg_date:
            messages.append("expiry_date must be after mfg_date")
        if mfg_date and mfg_date > date.today():
            messages.append("mfg_date is in the future")
        if expiry_date and expiry_date < date.today():
            messages.append("Product is already expired (expiry_date is in the past)")

        db.add(
            ImportStagingRow(
                batch_id=batch.id,
                line_number=i,
                parsed_values={
                    "sku": sku,
                    "name": product.name if product else None,
                    "product_id": str(product.id) if product else None,
                    "warehouse_id": str(warehouse_id) if warehouse_id else None,
                    "quantity": quantity,
                    "batch_number": batch_number,
                    "mfg_date": mfg_date.isoformat() if mfg_date else None,
                    "expiry_date": expiry_date.isoformat() if expiry_date else None,
                },
                computed_action="create" if not messages else "error",
                validation_messages=messages,
            )
        )
    await db.commit()
    return batch


async def commit_opening_stock_batch(db: AsyncSession, batch_id: uuid.UUID, current: CurrentUser) -> dict:
    batch = await get_batch_with_rows(db, batch_id)
    if batch is None or batch.status != "staged":
        raise ValueError("Batch not found or already applied")

    applied = 0
    batches_created = 0
    for row in batch.rows:
        if row.computed_action != "create" or row.validation_messages:
            continue
        v = row.parsed_values
        product_id = uuid.UUID(v["product_id"])
        warehouse_id = uuid.UUID(v["warehouse_id"]) if v.get("warehouse_id") else None
        if warehouse_id is None:
            await apply_movement(
                db,
                product_id=product_id,
                store_id=batch.store_id,
                delta=v["quantity"],
                reason_code="opening_stock",
                source_type="import_row",
                source_id=row.id,
                created_by=current.user_id,
                device_id=None,
            )
        else:
            await adjust_warehouse_balance(db, product_id=product_id, warehouse_id=warehouse_id, delta=v["quantity"])
        if v.get("expiry_date") or v.get("mfg_date") or v.get("batch_number"):
            product = await db.get(Product, product_id)
            db.add(
                InventoryBatch(
                    product_id=product.id,
                    store_id=batch.store_id if warehouse_id is None else None,
                    warehouse_id=warehouse_id,
                    batch_number=v.get("batch_number") or "OPENING",
                    mfg_date=date.fromisoformat(v["mfg_date"]) if v.get("mfg_date") else None,
                    expiry_date=date.fromisoformat(v["expiry_date"]) if v.get("expiry_date") else None,
                    quantity=v["quantity"],
                    purchase_cost=product.purchase_price or 0,
                )
            )
            batches_created += 1
        applied += 1

    batch.status = "applied"
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=batch.store_id,
        device_id=current.device_id,
        action="import.committed",
        entity_type="import_batch",
        entity_id=batch.id,
        new_value={"rows_applied": applied, "expiry_batches_created": batches_created},
    )
    await db.commit()
    return {"rows_applied": applied, "expiry_batches_created": batches_created}


def io_bytes(data: bytes):
    import io

    return io.BytesIO(data)
