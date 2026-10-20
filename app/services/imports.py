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
from app.models.models import Category, Customer, ImportBatch, ImportStagingRow, InventoryBalance, Product, ProductBarcode, Vendor
from app.models.models_phase2 import WarehouseBalance
from app.models.models_phase4 import InventoryBatch
from app.services.approvals import submit_or_apply
from app.services.audit import write_audit
from app.services.catalog import bump_revision
from app.services.inventory import adjust_warehouse_balance, apply_movement
from app.services.product_import_rules import (
    CUSTOMER_ALIASES,
    PRICE_FIELDS,
    PRODUCT_ALIASES,
    VENDOR_ALIASES,
    clean_text,
    describe_changes,
    diff_product,
    header_key,
    parse_product_row,
)

PRODUCT_COLUMNS = ["sku", "name", "barcode", "uom", "purchase_price", "selling_price", "mrp", "tax_rate", "hsn_code", "brand", "pack_size", "category"]
# batch_number, mfg_date and expiry_date are optional; when any is given the
# opening quantity is also recorded as an inventory batch so expiry tracking works.
# The location (store or warehouse) is picked on screen, not read from the sheet.
OPENING_STOCK_COLUMNS = ["sku", "quantity", "batch_number", "mfg_date", "expiry_date"]


def _blank_cell(raw) -> bool:
    return raw is None or str(raw).strip() == ""


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


def _rows_of(file_bytes: bytes, aliases: dict | None = None) -> tuple[list[str], list[tuple]]:
    if file_bytes[:2] == b"PK":  # an .xlsx file is a zip; anything else is read as CSV
        wb = openpyxl.load_workbook(io_bytes(file_bytes), read_only=True)
        rows = list(wb.active.iter_rows(values_only=True))
    else:
        import csv
        import io

        text_value = file_bytes.decode("utf-8-sig", errors="replace")
        rows = [tuple(r) for r in csv.reader(io.StringIO(text_value))]
    if not rows:
        raise ValueError("The file is empty")
    header = [header_key(h, PRODUCT_ALIASES if aliases is None else aliases) for h in rows[0]]
    return header, [r for r in rows[1:] if any(c is not None and str(c).strip() != "" for c in r)]


def _product_snapshot(p: Product, category_names: dict | None = None) -> dict:
    return {
        "brand": p.brand, "pack_size": float(p.pack_size or 1),
        "category": (category_names or {}).get(p.category_id),
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
        found = ", ".join(h for h in header if h) or "none"
        raise ValueError(f"The first row must have a column called sku (this file has: {found})")

    batch = ImportBatch(import_type="products", uploaded_by=uploaded_by, file_name=filename, file_bytes=file_bytes, status="staged")
    db.add(batch)
    await db.flush()

    all_products = list((await db.execute(select(Product))).scalars().all())
    category_names = {c.id: c.name for c in (await db.execute(select(Category))).scalars().all()}
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
            changes = diff_product(_product_snapshot(existing, category_names), provided)
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
        elif row.computed_action == "create" and batch.import_type == "other_prices":
            cost = f", cost {v['purchase_price']}" if v.get("purchase_price") is not None else ""
            detail = [f"Add price {v.get('selling_price')} (MRP {v.get('mrp')}{cost}) - {v.get('label')}"]
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
    categories = {c.name.strip().lower(): c for c in (await db.execute(select(Category))).scalars().all()}

    async def category_id(name: str | None):
        """The id of the category with this name (any capitals), made on the spot when it is new."""
        if not name:
            return None
        found = categories.get(name.strip().lower())
        if found is None:
            found = Category(name=name.strip())
            db.add(found)
            await db.flush()
            categories[name.strip().lower()] = found
        return found.id

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
                brand=v.get("brand"), pack_size=v.get("pack_size", 1), category_id=await category_id(v.get("category")),
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
            if field == "category":
                product.category_id = await category_id(change["new"])
            else:
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
        found = ", ".join(h for h in header if h) or "none"
        raise ValueError(f"The first row must have the columns sku and quantity (this file has: {found}). Prices go in the Other Prices card.")

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


async def stage_other_prices_file(db: AsyncSession, *, file_bytes: bytes, filename: str, uploaded_by: uuid.UUID) -> ImportBatch:
    """Extra prices (another batch / old stock) for products that already exist, matched by SKU.
    One row = one extra price; the same SKU can appear on several rows (one per price)."""
    try:
        header, data_rows = _rows_of(file_bytes)
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ValueError("Could not read this file - upload an .xlsx or .csv file") from exc
    if not {"sku", "mrp", "selling_price"} <= set(header):
        found = ", ".join(h for h in header if h) or "none"
        raise ValueError(f"The first row must have the columns sku, mrp and selling_price (this file has: {found})")

    batch = ImportBatch(import_type="other_prices", uploaded_by=uploaded_by, file_name=filename, file_bytes=file_bytes, status="staged")
    db.add(batch)
    await db.flush()

    products = {p.sku: p for p in (await db.execute(select(Product))).scalars().all()}
    seen: set[tuple[str, float, float]] = set()

    for i, row in enumerate(data_rows, start=2):
        values = dict(zip(header, row, strict=False))
        messages: list[str] = []
        sku = clean_text(values.get("sku")) or ""
        product = products.get(sku)
        if not sku:
            messages.append("SKU is missing")
        elif product is None:
            messages.append(f"Unknown SKU {sku} - add the product first")
        numbers: dict[str, float] = {}
        for field in ("mrp", "selling_price"):
            try:
                number = float(values.get(field))
                if number <= 0:
                    messages.append(f"{field} must be more than 0")
                numbers[field] = round(number, 2)
            except (TypeError, ValueError):
                messages.append(f"{field} is not a number")
        mrp, selling = numbers.get("mrp"), numbers.get("selling_price")
        if mrp is not None and selling is not None:
            if selling > mrp:
                messages.append("The selling price cannot be above the MRP")
            key = (sku, mrp, selling)
            if key in seen:
                messages.append("This price appears twice for this SKU in the file")
            seen.add(key)
            if product is not None:
                if abs(float(product.mrp) - mrp) < 0.005 and abs(float(product.selling_price) - selling) < 0.005:
                    messages.append("This is already the product's current price")
                elif any(v.is_active and abs(float(v.mrp) - mrp) < 0.005 and abs(float(v.selling_price) - selling) < 0.005 for v in product.price_variants):
                    messages.append("The product already has this price")
        label = (clean_text(values.get("label")) or "Old stock")[:40]
        cost = None
        if not _blank_cell(values.get("purchase_price")):
            try:
                cost = round(float(values.get("purchase_price")), 2)
                if cost < 0:
                    messages.append("purchase_price cannot be negative")
            except (TypeError, ValueError):
                messages.append("purchase_price is not a number")

        db.add(
            ImportStagingRow(
                batch_id=batch.id,
                line_number=i,
                parsed_values={
                    "sku": sku,
                    "name": product.name if product else None,
                    "product_id": str(product.id) if product else None,
                    "mrp": mrp,
                    "selling_price": selling,
                    "purchase_price": cost,
                    "label": label,
                },
                computed_action="create" if not messages else "error",
                validation_messages=messages,
            )
        )
    await db.commit()
    return batch


async def commit_other_prices_batch(db: AsyncSession, batch_id: uuid.UUID, current: CurrentUser) -> dict:
    """Each row is an ordinary 'add another price' request: a Super Admin's apply at once, others wait for approval."""
    batch = await get_batch_with_rows(db, batch_id)
    if batch is None or batch.status != "staged" or batch.import_type != "other_prices":
        raise ValueError("Batch not found or already applied")

    added, pending = 0, 0
    for row in batch.rows:
        if row.computed_action != "create" or row.validation_messages:
            continue
        v = row.parsed_values
        product = await db.get(Product, uuid.UUID(v["product_id"]))
        if product is None:
            continue
        request = await submit_or_apply(
            db,
            current=current,
            request_type="price_variant_add",
            entity_type="product",
            entity_id=product.id,
            old_value={"selling_price": float(product.selling_price), "mrp": float(product.mrp)},
            new_value={
                "mrp": v["mrp"], "selling_price": v["selling_price"], "label": v["label"],
                **({"purchase_price": v["purchase_price"]} if v.get("purchase_price") is not None else {}),
            },
            reason=f"Other price from import {batch.file_name}",
            store_id=None,
        )
        if request.status == "approved":
            added += 1
        else:
            pending += 1

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
        new_value={"prices_added": added, "pending_approval": pending},
    )
    await db.commit()
    return {"prices_added": added, "prices_pending_approval": pending}


# ---------------------------------------------------------------------------
# Customers and vendors: simple masters, matched on a key, blank cell = leave as is
# ---------------------------------------------------------------------------

import re as _re

_PHONE_DIGITS = _re.compile(r"\D+")


def _clean_phone(raw) -> str | None:
    """Excel hands phone numbers back as 9876543210.0 or with spaces / +91; keep the last 10 digits of an Indian mobile."""
    text_value = clean_text(raw)
    if text_value is None:
        return None
    digits = _PHONE_DIGITS.sub("", text_value)
    if len(digits) > 10 and digits.startswith("91"):
        digits = digits[-10:]
    return digits or None


VENDOR_TEXT = ("name", "gst_number", "phone", "email", "category", "service_area", "contact_person", "alt_phone", "legal_name",
               "pan_number", "address_line1", "address_line2", "city", "state", "pincode", "bank_name", "bank_account_name",
               "bank_account_number", "bank_ifsc")


def _diff_fields(obj, provided: dict) -> dict:
    changes = {}
    for field, new in provided.items():
        old = getattr(obj, field, None)
        if (old or None) != new:
            changes[field] = {"old": old, "new": new}
    return changes


async def stage_customers_file(db: AsyncSession, *, file_bytes: bytes, filename: str, uploaded_by: uuid.UUID) -> ImportBatch:
    """Customers by mobile number: new numbers are added, known ones get the name / email that the sheet fills in."""
    try:
        header, data_rows = _rows_of(file_bytes, CUSTOMER_ALIASES)
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ValueError("Could not read this file - upload an .xlsx or .csv file") from exc
    if "phone" not in header:
        found = ", ".join(h for h in header if h) or "none"
        raise ValueError(f"The first row must have a phone (or mobile) column (this file has: {found})")

    batch = ImportBatch(import_type="customers", uploaded_by=uploaded_by, file_name=filename, file_bytes=file_bytes, status="staged")
    db.add(batch)
    await db.flush()
    by_phone = {c.phone: c for c in (await db.execute(select(Customer))).scalars().all()}
    seen: set[str] = set()

    for i, row in enumerate(data_rows, start=2):
        values = dict(zip(header, row, strict=False))
        messages: list[str] = []
        phone = _clean_phone(values.get("phone"))
        provided: dict = {}
        if not phone:
            messages.append("Phone number is missing")
        elif len(phone) != 10:
            messages.append(f"Phone {phone} is not a 10-digit mobile number")
        elif phone in seen:
            messages.append("This phone number appears twice in the file")
        seen.add(phone or "")
        for field in ("name", "email"):
            value = clean_text(values.get(field))
            if value is not None:
                if field == "email" and "@" not in value:
                    messages.append(f"Email {value} is not valid")
                    continue
                provided[field] = value
        existing = by_phone.get(phone or "")
        changes: dict = {}
        if messages:
            action = "error"
        elif existing is None:
            action = "create"
        else:
            changes = _diff_fields(existing, provided)
            action = "update" if changes else "skip"
        db.add(
            ImportStagingRow(
                batch_id=batch.id, line_number=i,
                parsed_values={"sku": phone, "phone": phone, "label": provided.get("name") or existing.name if existing else provided.get("name"), **provided, "_changes": changes},
                computed_action=action, validation_messages=messages,
            )
        )
    await db.commit()
    return batch


async def commit_customers_batch(db: AsyncSession, batch_id: uuid.UUID, current: CurrentUser) -> dict:
    batch = await get_batch_with_rows(db, batch_id)
    if batch is None or batch.status != "staged" or batch.import_type != "customers":
        raise ValueError("Batch not found or already applied")
    by_phone = {c.phone: c for c in (await db.execute(select(Customer))).scalars().all()}
    created = updated = 0
    for row in batch.rows:
        if row.computed_action not in ("create", "update") or row.validation_messages:
            continue
        v = row.parsed_values
        existing = by_phone.get(v["phone"])
        if existing is None:
            customer = Customer(phone=v["phone"], name=v.get("name"), email=v.get("email"))
            db.add(customer)
            by_phone[v["phone"]] = customer
            created += 1
        else:
            for field, change in (v.get("_changes") or {}).items():
                setattr(existing, field, change["new"])
            updated += 1
    batch.status = "applied"
    await write_audit(
        db, user_id=current.user_id, role_code=current.role_code, store_id=None, device_id=current.device_id,
        action="import.committed", entity_type="import_batch", entity_id=batch.id, new_value={"created": created, "updated": updated},
    )
    await db.commit()
    return {"created": created, "updated": updated}


async def stage_vendors_file(db: AsyncSession, *, file_bytes: bytes, filename: str, uploaded_by: uuid.UUID) -> ImportBatch:
    """Vendors matched by GST number, else by name (any capitals). Blank cells leave a vendor's details alone."""
    try:
        header, data_rows = _rows_of(file_bytes, VENDOR_ALIASES)
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ValueError("Could not read this file - upload an .xlsx or .csv file") from exc
    if "name" not in header and "gst_number" not in header:
        found = ", ".join(h for h in header if h) or "none"
        raise ValueError(f"The first row must have a name (or gst_number) column (this file has: {found})")

    batch = ImportBatch(import_type="vendors", uploaded_by=uploaded_by, file_name=filename, file_bytes=file_bytes, status="staged")
    db.add(batch)
    await db.flush()
    vendors = list((await db.execute(select(Vendor))).scalars().all())
    by_gst = {v.gst_number.upper(): v for v in vendors if v.gst_number}
    by_name = {v.name.strip().lower(): v for v in vendors}
    seen: set[str] = set()

    for i, row in enumerate(data_rows, start=2):
        values = dict(zip(header, row, strict=False))
        messages: list[str] = []
        provided: dict = {}
        for field in VENDOR_TEXT:
            if field in ("phone", "alt_phone"):
                value = _clean_phone(values.get(field))
            else:
                value = clean_text(values.get(field))
            if value is not None:
                provided[field] = value.upper() if field in ("gst_number", "pan_number", "bank_ifsc") else value
        if not _blank_cell(values.get("credit_days")):
            try:
                days = int(float(str(values.get("credit_days")).lower().replace("days", "").strip()))
                if days < 0:
                    raise ValueError
                provided["credit_days"] = days
            except ValueError:
                messages.append("credit_days must be a whole number of days")
        gst = provided.get("gst_number")
        if gst and len(gst) != 15:
            messages.append(f"GST number {gst} should have 15 characters")
        existing = by_gst.get(gst) if gst else None
        if existing is None and provided.get("name"):
            existing = by_name.get(provided["name"].strip().lower())
        if existing is None and not provided.get("name"):
            messages.append("Name is required for a new vendor")
        key = (gst or provided.get("name", "")).lower()
        if key in seen:
            messages.append("This vendor appears twice in the file")
        seen.add(key)
        if gst and existing is None and gst in by_gst:
            messages.append(f"GST number {gst} already belongs to another vendor")
        changes: dict = {}
        if messages:
            action = "error"
        elif existing is None:
            action = "create"
        else:
            changes = _diff_fields(existing, provided)
            action = "update" if changes else "skip"
        db.add(
            ImportStagingRow(
                batch_id=batch.id, line_number=i,
                parsed_values={
                    "sku": gst or provided.get("phone"), "label": provided.get("name") or (existing.name if existing else None),
                    "vendor_id": str(existing.id) if existing else None, **provided, "_changes": changes,
                },
                computed_action=action, validation_messages=messages,
            )
        )
    await db.commit()
    return batch


async def commit_vendors_batch(db: AsyncSession, batch_id: uuid.UUID, current: CurrentUser) -> dict:
    batch = await get_batch_with_rows(db, batch_id)
    if batch is None or batch.status != "staged" or batch.import_type != "vendors":
        raise ValueError("Batch not found or already applied")
    created = updated = 0
    for row in batch.rows:
        if row.computed_action not in ("create", "update") or row.validation_messages:
            continue
        v = row.parsed_values
        if row.computed_action == "create":
            fields = {k: val for k, val in v.items() if k in VENDOR_TEXT or k == "credit_days"}
            db.add(Vendor(**fields))
            created += 1
        else:
            vendor = await db.get(Vendor, uuid.UUID(v["vendor_id"]))
            if vendor is None:
                continue
            for field, change in (v.get("_changes") or {}).items():
                setattr(vendor, field, change["new"])
            updated += 1
    batch.status = "applied"
    await write_audit(
        db, user_id=current.user_id, role_code=current.role_code, store_id=None, device_id=current.device_id,
        action="import.committed", entity_type="import_batch", entity_id=batch.id, new_value={"created": created, "updated": updated},
    )
    await db.commit()
    return {"created": created, "updated": updated}


def io_bytes(data: bytes):
    import io

    return io.BytesIO(data)
