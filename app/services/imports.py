"""Excel import never touches a business table directly. The file is parsed into
staging rows; the user sees a preview; only on confirmation does a single transaction
apply the valid rows, rolling back entirely on failure.

Three hard rules: no deletes via import ever (a missing row means nothing); price
changes via import route through the approval engine as one request holding every
delta; opening stock only imports into an empty balance.
"""

import uuid

import openpyxl
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser
from app.models.models import ImportBatch, ImportStagingRow, InventoryBalance, Product
from app.services.approvals import submit_or_apply
from app.services.audit import write_audit
from app.services.inventory import apply_movement

PRODUCT_COLUMNS = ["sku", "name", "barcode", "uom", "purchase_price", "selling_price", "mrp", "tax_rate"]
OPENING_STOCK_COLUMNS = ["sku", "store_code", "quantity"]


async def stage_products_file(db: AsyncSession, *, file_bytes: bytes, filename: str, uploaded_by: uuid.UUID) -> ImportBatch:
    wb = openpyxl.load_workbook(io_bytes(file_bytes), read_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    header, *data_rows = rows

    batch = ImportBatch(import_type="products", uploaded_by=uploaded_by, file_name=filename, file_bytes=file_bytes, status="staged")
    db.add(batch)
    await db.flush()

    existing_skus = {p.sku: p for p in (await db.execute(select(Product))).scalars().all()}

    for i, row in enumerate(data_rows, start=2):
        values = dict(zip(header, row, strict=False))
        messages: list[str] = []
        sku = str(values.get("sku") or "").strip()
        if not sku:
            messages.append("Missing SKU")
        try:
            selling_price = float(values.get("selling_price") or 0)
            if selling_price < 0:
                messages.append("selling_price must be >= 0")
        except (TypeError, ValueError):
            messages.append("selling_price is not numeric")
            selling_price = 0
        try:
            tax_rate = float(values.get("tax_rate") or 0)
        except (TypeError, ValueError):
            messages.append("tax_rate is not numeric")
            tax_rate = 0

        action = "error"
        if not messages:
            action = "update" if sku in existing_skus else "create"

        db.add(
            ImportStagingRow(
                batch_id=batch.id,
                line_number=i,
                parsed_values={**{k: values.get(k) for k in PRODUCT_COLUMNS}, "selling_price": selling_price, "tax_rate": tax_rate},
                computed_action=action,
                validation_messages=messages,
            )
        )
    await db.commit()
    return batch


async def preview_batch(db: AsyncSession, batch_id: uuid.UUID) -> dict:
    batch = await db.get(ImportBatch, batch_id)
    counts = {"create": 0, "update": 0, "error": 0, "skip": 0}
    for row in batch.rows:
        counts[row.computed_action or "error"] = counts.get(row.computed_action or "error", 0) + 1
    return {"batch_id": str(batch_id), "counts": counts, "total_rows": len(batch.rows)}


async def commit_products_batch(db: AsyncSession, batch_id: uuid.UUID, current: CurrentUser) -> dict:
    batch = await db.get(ImportBatch, batch_id)
    if batch is None or batch.status != "staged":
        raise ValueError("Batch not found or already applied")

    valid_rows = [r for r in batch.rows if r.computed_action in ("create", "update") and not r.validation_messages]
    price_deltas = []
    created, updated = 0, 0

    for row in valid_rows:
        v = row.parsed_values
        result = await db.execute(select(Product).where(Product.sku == v["sku"]))
        product = result.scalar_one_or_none()
        if product is None:
            product = Product(
                sku=v["sku"], name=v.get("name") or v["sku"], barcode=v.get("barcode"),
                uom=v.get("uom") or "EA", selling_price=v["selling_price"], mrp=v.get("mrp") or v["selling_price"],
                tax_rate=v["tax_rate"], purchase_price=v.get("purchase_price") or 0,
            )
            db.add(product)
            created += 1
        else:
            if float(product.selling_price) != float(v["selling_price"]):
                price_deltas.append({"sku": v["sku"], "old": float(product.selling_price), "new": v["selling_price"]})
            product.name = v.get("name") or product.name
            product.tax_rate = v["tax_rate"]
            updated += 1

    # Bulk repricing becomes one approval request holding every delta, not a silent change.
    if price_deltas:
        await submit_or_apply(
            db,
            current=current,
            request_type="config_change",
            entity_type="import_batch",
            entity_id=batch.id,
            old_value=None,
            new_value={"price_deltas": price_deltas},
            reason=f"Bulk price changes from import {batch.file_name}",
            store_id=None,
        )

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
        new_value={"created": created, "updated": updated, "price_deltas": len(price_deltas)},
    )
    await db.commit()
    return {"created": created, "updated": updated, "price_changes_pending_approval": len(price_deltas)}


async def stage_opening_stock_file(db: AsyncSession, *, file_bytes: bytes, filename: str, store_id: uuid.UUID, uploaded_by: uuid.UUID) -> ImportBatch:
    wb = openpyxl.load_workbook(io_bytes(file_bytes), read_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    header, *data_rows = rows

    batch = ImportBatch(
        import_type="opening_stock", store_id=store_id, uploaded_by=uploaded_by, file_name=filename,
        file_bytes=file_bytes, status="staged",
    )
    db.add(batch)
    await db.flush()

    products = {p.sku: p for p in (await db.execute(select(Product))).scalars().all()}
    existing_balances = {
        b.product_id for b in (await db.execute(select(InventoryBalance).where(InventoryBalance.store_id == store_id))).scalars().all()
    }

    for i, row in enumerate(data_rows, start=2):
        values = dict(zip(header, row, strict=False))
        messages: list[str] = []
        sku = str(values.get("sku") or "").strip()
        product = products.get(sku)
        if product is None:
            messages.append(f"Unknown SKU {sku}")
        elif product.id in existing_balances:
            messages.append("Opening stock already set for this product/store — re-run is a likely duplicate")
        try:
            quantity = float(values.get("quantity") or 0)
        except (TypeError, ValueError):
            messages.append("quantity is not numeric")
            quantity = 0

        db.add(
            ImportStagingRow(
                batch_id=batch.id,
                line_number=i,
                parsed_values={"sku": sku, "product_id": str(product.id) if product else None, "quantity": quantity},
                computed_action="create" if not messages else "error",
                validation_messages=messages,
            )
        )
    await db.commit()
    return batch


async def commit_opening_stock_batch(db: AsyncSession, batch_id: uuid.UUID, current: CurrentUser) -> dict:
    batch = await db.get(ImportBatch, batch_id)
    if batch is None or batch.status != "staged":
        raise ValueError("Batch not found or already applied")

    applied = 0
    for row in batch.rows:
        if row.computed_action != "create" or row.validation_messages:
            continue
        v = row.parsed_values
        await apply_movement(
            db,
            product_id=uuid.UUID(v["product_id"]),
            store_id=batch.store_id,
            delta=v["quantity"],
            reason_code="opening_stock",
            source_type="import_row",
            source_id=row.id,
            created_by=current.user_id,
            device_id=None,
        )
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
        new_value={"rows_applied": applied},
    )
    await db.commit()
    return {"rows_applied": applied}


def io_bytes(data: bytes):
    import io

    return io.BytesIO(data)
