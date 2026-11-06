import io
import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from openpyxl import Workbook
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, get_current_user, require_permission, require_store_access, require_warehouse_access
from app.core.database import get_db
from app.models.models import ImportBatch
from app.services import imports as import_service

router = APIRouter(prefix="/imports", tags=["imports"])


TEMPLATES = {
    "products": {
        "permission": "import.product.import",
        "sheet": "Products",
        "file": "product-import-template.xlsx",
        "columns": ["sku", "name", "barcode", "uom", "purchase_price", "selling_price", "mrp", "tax_rate", "hsn_code", "brand", "category", "pack_size", "other_barcodes"],
        "examples": [
            ["EXAMPLE-001", "Cadbury Bournvita 500g (example - delete this row)", "8901233012345", "EA", 215, 240, 250, 18, "1806", "Cadbury", "Health drinks", 500, "8901233099999, 8901233088888"],
            ["EXAMPLE-002", "Tata Salt 1kg (example - delete this row)", "8901000000002", "EA", 18, 22, 25, 5, "2501", "Tata", "Staples", 1000, ""],
        ],
        "help": [
            ("sku", "Yes", "Your own unique code for the product. This is how the file finds an existing product.", "BOURN-500"),
            ("name", "New products", "The name shown on bills and screens. Leave blank on an existing product to keep its name.", "Cadbury Bournvita 500g"),
            ("barcode", "No", "The main barcode. Must not belong to another product.", "8901233012345"),
            ("uom", "No", "Unit: EA (each), KG, G, L, ML, PKT, BOX, DOZ.", "EA"),
            ("purchase_price", "No", "What you pay the vendor, per unit.", "215"),
            ("selling_price", "New products", "Price the customer pays, including GST. Changing it on an existing product goes for approval (Super Admin: applied at once).", "240"),
            ("mrp", "No", "The printed price. Cannot be lower than the selling price. Empty = same as selling price.", "250"),
            ("tax_rate", "No", "GST percentage: 0, 5, 12, 18 or 28.", "18"),
            ("hsn_code", "No", "HSN code for GST invoices.", "1806"),
            ("brand", "No", "The brand name.", "Cadbury"),
            ("category", "No", "The category. A new name is created for you; a known one is matched ignoring capitals.", "Health drinks"),
            ("pack_size", "No", "Size of one pack, in the unit's own measure (e.g. 500 for a 500 g pack). Must be more than 0.", "500"),
            ("other_barcodes", "No", "More barcodes of the same product, separated by commas. Scanning any of them finds the product. Only added, never removed.", "8901233099999, 8901233088888"),
        ],
        "notes": [
            "A blank cell never changes anything: only the cells you fill in are updated.",
            "Column names are flexible: Item Name, MRP, Cost, GST %, HSN, Brand, Category... are all understood. Any column the importer does not know is ignored.",
            "Delete the two EXAMPLE rows before you upload.",
            "You will see a row-by-row preview first. Nothing is saved until you press Save.",
        ],
    },
    "other-prices": {
        "permission": "import.product.import",
        "sheet": "Other prices",
        "file": "other-prices-import-template.xlsx",
        "columns": ["sku", "mrp", "selling_price", "purchase_price", "label"],
        "examples": [
            ["EXAMPLE-001", 250, 240, 200, "Old stock"],
            ["EXAMPLE-001", 245, 235, 195, "Batch B2409"],
        ],
        "help": [
            ("sku", "Yes", "The SKU of a product that already exists. The same SKU can be on several rows, one per price.", "BOURN-500"),
            ("mrp", "Yes", "The MRP printed on this batch.", "250"),
            ("selling_price", "Yes", "What the customer pays for this batch. Cannot be above the MRP.", "240"),
            ("purchase_price", "No", "What this batch cost you, per unit. Empty = same cost as the product.", "200"),
            ("label", "No", "A short name the cashier sees when picking the price. Empty = Old stock.", "Old stock"),
        ],
        "notes": [
            "The product keeps its current price too; billing asks which price is on the pack.",
            "Like any price change, a Super Admin's rows apply at once and anyone else's wait for approval.",
            "Set how many units are at each price afterwards, on the product's Price screen.",
            "A .csv file with the same columns works too.",
            "Delete the two EXAMPLE rows before you upload.",
        ],
    },
    "customers": {
        "permission": "import.product.import",
        "sheet": "Customers",
        "file": "customers-import-template.xlsx",
        "columns": ["phone", "name", "email"],
        "examples": [["9876543210", "Asha Verma (example - delete this row)", "asha@example.com"], ["9123456780", "Ravi Kumar (example - delete this row)", ""]],
        "help": [
            ("phone", "Yes", "10-digit mobile number. This is how the file finds an existing customer. +91 and spaces are removed for you.", "9876543210"),
            ("name", "No", "Customer name.", "Asha Verma"),
            ("email", "No", "Email address.", "asha@example.com"),
        ],
        "notes": [
            "A new mobile number adds a customer; a known one gets the name or email you fill in. A blank cell changes nothing.",
            "Column names are flexible: Mobile, Customer Name, Email ID... are understood. Other columns are ignored.",
            "Delete the two EXAMPLE rows before you upload.",
        ],
    },
    "vendors": {
        "permission": "import.product.import",
        "sheet": "Vendors",
        "file": "vendors-import-template.xlsx",
        "columns": ["name", "gst_number", "phone", "email", "contact_person", "credit_days", "category", "address_line1", "city", "state", "pincode", "pan_number"],
        "examples": [
            ["ABC Distributors (example - delete this row)", "29ABCDE1234F1Z5", "9876501234", "sales@abc.example", "Mr Rao", 30, "FMCG", "12 Market Road", "Bengaluru", "Karnataka", "560001", "ABCDE1234F"],
        ],
        "help": [
            ("name", "New vendors", "Vendor name. Also used to find an existing vendor when there is no GST number.", "ABC Distributors"),
            ("gst_number", "No", "15-character GSTIN. The best way to find an existing vendor.", "29ABCDE1234F1Z5"),
            ("phone", "No", "Mobile or phone number.", "9876501234"),
            ("email", "No", "Email address.", "sales@abc.example"),
            ("contact_person", "No", "Who you deal with.", "Mr Rao"),
            ("credit_days", "No", "Days of credit the vendor gives you.", "30"),
            ("category", "No", "What they supply.", "FMCG"),
            ("address_line1", "No", "Address, plus city, state, pincode in their own columns.", "12 Market Road"),
            ("pan_number", "No", "PAN, for purchase bills.", "ABCDE1234F"),
        ],
        "notes": [
            "Also understood: address_line2, legal_name, alt_phone, service_area, bank_name, bank_account_name, bank_account_number, bank_ifsc.",
            "Column names are flexible: Supplier, GSTIN, Mobile, Credit Period... are understood. Other columns are ignored.",
            "A blank cell changes nothing. Delete the EXAMPLE row before you upload.",
        ],
    },
    "opening-stock": {
        "permission": "import.opening_stock.import",
        "sheet": "Opening stock",
        "file": "opening-stock-template.xlsx",
        "columns": ["sku", "quantity", "batch_number", "mfg_date", "expiry_date", "purchase_price"],
        "examples": [
            ["EXAMPLE-001", 120, "B2410", "2026-09-01", "2027-03-01", 62.566],
            ["EXAMPLE-002", 300, "", "", "", ""],
        ],
        "help": [
            ("sku", "Yes", "The SKU of a product that already exists (add it in Products first).", "BOURN-500"),
            ("quantity", "Yes", "How many units are on the shelf now. Must be more than 0.", "120"),
            ("batch_number", "No", "Batch number, for items that expire.", "B2410"),
            ("mfg_date", "No", "Manufacturing date: YYYY-MM-DD or DD-MM-YYYY.", "2026-09-01"),
            ("expiry_date", "No", "Expiry date. Fill batch and dates for perishables so expiry is tracked.", "2027-03-01"),
            ("purchase_price", "No", "What one unit cost (before GST). It is recorded on the batch; blank uses the product's current cost.", "62.566"),
        ],
        "notes": [
            "Pick the store or warehouse on the Import Data screen - it is not a column in the file.",
            "Opening stock can be loaded once per product and place. After that use Purchases, Transfers or a stock count.",
            "Delete the two EXAMPLE rows before you upload.",
        ],
    },
    "purchase-bill": {
        "permission": "procurement.purchase.create",
        "sheet": "Purchase bill",
        "file": "purchase-bill-template.xlsx",
        "columns": ["sku", "bill_serial", "mrp", "batch_no", "hsn_code", "receipt", "free", "reject", "hl", "rate", "discount", "gst", "cess", "surcharge_rate"],
        "examples": [
            ["EXAMPLE-001", 1, 10, "B2410", "0910", 10, "", "", "", 6.857, "", 5, "", ""],
            ["EXAMPLE-002", 2, 78, "B2411", "0910", 5, 1, "", "", 54.88, 10, 5, "", ""],
        ],
        "help": [
            ("sku", "Yes", "The item code (SKU) of a product that already exists. A barcode also works.", "BOURN-500"),
            ("receipt", "Yes", "The quantity on the vendor's bill. Must be more than 0.", "10"),
            ("rate", "Yes", "The vendor's rate per unit BEFORE GST. Up to 4 decimals.", "6.857"),
            ("bill_serial", "No", "The line's serial number on the vendor's bill.", "1"),
            ("mrp", "No", "MRP printed on the pack for this lot.", "10"),
            ("batch_no", "No", "Batch number. It also puts the lot in Check Stock's batch list.", "B2410"),
            ("hsn_code", "No", "HSN code. Blank uses the product's.", "0910"),
            ("free", "No", "Units given free. They go into stock and cost nothing.", "1"),
            ("reject", "No", "Units that arrived but were refused. They never enter stock.", "2"),
            ("hl", "No", "Units broken or leaked (H.L.). They never enter stock.", "1"),
            ("discount", "No", "Discount in rupees on this line, taken off before GST.", "10"),
            ("gst", "No", "GST code in percent (0, 5, 12, 18, 28, 40). Blank uses the product's rate.", "5"),
            ("cess", "No", "Additional cess in rupees on this line.", "0"),
            ("surcharge_rate", "No", "Surcharge in percent of the amount after discount.", "0"),
        ],
        "notes": [
            "Pick the store, the vendor, the vendor's bill number and the bill date on the Import Data screen. They are not columns in the file.",
            "One file is one vendor bill. It is saved whole or not at all: if any line has a problem, fix the file and upload it again.",
            "Amount = receipt x rate. GST = (amount - discount) x GST code. Net = amount - discount + GST + cess + surcharge. Stock in = receipt + free - reject - H.L.",
            "Column names are flexible: Item Code, Qty, Batch, HSN, Disc, GST %, H.L. ... are understood. Delete the two EXAMPLE rows before you upload.",
        ],
    },
}


@router.get("/templates/{kind}")
async def download_template(
    kind: str,
    current: CurrentUser = Depends(get_current_user),
) -> StreamingResponse:
    """A ready-to-fill Excel file: the right columns, two example rows, and a "How to fill" sheet."""
    spec = TEMPLATES.get(kind)
    if spec is None:
        raise HTTPException(status_code=404, detail="Unknown template")
    if not current.has_permission(spec["permission"]):
        raise HTTPException(status_code=403, detail=f"Permission denied: {spec['permission']}")
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = spec["sheet"]
    ws.append(spec["columns"])
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F2937")
    for row in spec["examples"]:
        ws.append(row)
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.font = Font(italic=True, color="9CA3AF")
    for idx, column in enumerate(spec["columns"], start=1):
        ws.column_dimensions[ws.cell(row=1, column=idx).column_letter].width = max(14, len(column) + 6)
    ws.column_dimensions["B"].width = 42
    ws.freeze_panes = "A2"

    guide = wb.create_sheet("How to fill")
    guide.append(["Column", "Needed?", "What to put", "Example"])
    for cell in guide[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F2937")
    for row in spec["help"]:
        guide.append(list(row))
    guide.append([])
    for note in spec["notes"]:
        guide.append([note])
    for col, width in zip("ABCD", (18, 14, 90, 34)):
        guide.column_dimensions[col].width = width
    for row in guide.iter_rows():
        for cell in row:
            cell.alignment = Alignment(wrap_text=True, vertical="top")

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{spec["file"]}"'},
    )


@router.post("/products/stage")
async def stage_products(
    file: UploadFile,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("import.product.import")),
) -> dict:
    content = await file.read()
    try:
        batch = await import_service.stage_products_file(db, file_bytes=content, filename=file.filename, uploaded_by=current.user_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await import_service.preview_batch(db, batch.id)


@router.post("/other-prices/stage")
async def stage_other_prices(
    file: UploadFile,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("import.product.import")),
) -> dict:
    content = await file.read()
    try:
        batch = await import_service.stage_other_prices_file(db, file_bytes=content, filename=file.filename, uploaded_by=current.user_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await import_service.preview_batch(db, batch.id)


@router.post("/other-prices/{batch_id}/commit")
async def commit_other_prices(
    batch_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("import.product.import")),
) -> dict:
    try:
        return await import_service.commit_other_prices_batch(db, batch_id, current)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/customers/stage")
async def stage_customers(
    file: UploadFile,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("import.product.import")),
) -> dict:
    content = await file.read()
    try:
        batch = await import_service.stage_customers_file(db, file_bytes=content, filename=file.filename, uploaded_by=current.user_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await import_service.preview_batch(db, batch.id)


@router.post("/customers/{batch_id}/commit")
async def commit_customers(
    batch_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("import.product.import")),
) -> dict:
    try:
        return await import_service.commit_customers_batch(db, batch_id, current)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/vendors/stage")
async def stage_vendors(
    file: UploadFile,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("import.product.import")),
) -> dict:
    content = await file.read()
    try:
        batch = await import_service.stage_vendors_file(db, file_bytes=content, filename=file.filename, uploaded_by=current.user_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await import_service.preview_batch(db, batch.id)


@router.post("/vendors/{batch_id}/commit")
async def commit_vendors(
    batch_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("import.product.import")),
) -> dict:
    try:
        return await import_service.commit_vendors_batch(db, batch_id, current)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/{batch_id}/preview")
async def preview(
    batch_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("import.batch.view")),
) -> dict:
    return await import_service.preview_batch(db, batch_id)


@router.post("/products/{batch_id}/commit")
async def commit_products(
    batch_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("import.product.import")),
) -> dict:
    try:
        return await import_service.commit_products_batch(db, batch_id, current)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/purchase-bill/stage")
async def stage_purchase_bill(
    file: UploadFile,
    store_id: uuid.UUID,
    vendor_id: uuid.UUID,
    invoice_number: str | None = None,
    invoice_date: date | None = None,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("procurement.purchase.create")),
) -> dict:
    """One vendor bill from a file, shown as a preview first. Nothing is saved until the commit."""
    require_store_access(store_id, current)
    content = await file.read()
    try:
        batch = await import_service.stage_purchase_bill_file(
            db, file_bytes=content, filename=file.filename, uploaded_by=current.user_id, store_id=store_id, vendor_id=vendor_id,
            invoice_number=(invoice_number or "").strip() or None, invoice_date=invoice_date,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await import_service.preview_batch(db, batch.id)


@router.post("/purchase-bill/{batch_id}/commit")
async def commit_purchase_bill(
    batch_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("procurement.purchase.create")),
) -> dict:
    batch = await db.get(ImportBatch, batch_id)
    if batch is not None and batch.store_id is not None:
        require_store_access(batch.store_id, current)
    try:
        return await import_service.commit_purchase_bill_batch(db, batch_id, current)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/opening-stock/stage")
async def stage_opening_stock(
    file: UploadFile,
    store_id: uuid.UUID | None = None,
    warehouse_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("import.opening_stock.import")),
) -> dict:
    """Opening stock for one store OR one warehouse (exactly one of the two)."""
    if (store_id is None) == (warehouse_id is None):
        raise HTTPException(status_code=400, detail="Pick either a store or a warehouse to load stock into")
    if store_id is not None:
        require_store_access(store_id, current)
    else:
        require_warehouse_access(warehouse_id, current)
    content = await file.read()
    try:
        batch = await import_service.stage_opening_stock_file(
            db, file_bytes=content, filename=file.filename, store_id=store_id, warehouse_id=warehouse_id, uploaded_by=current.user_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await import_service.preview_batch(db, batch.id)


@router.post("/opening-stock/{batch_id}/commit")
async def commit_opening_stock(
    batch_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("import.opening_stock.import")),
) -> dict:
    try:
        return await import_service.commit_opening_stock_batch(db, batch_id, current)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/{batch_id}/file")
async def download_original_file(
    batch_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("import.batch.view")),
) -> StreamingResponse:
    """Every batch keeps its uploaded file (§10) — this is how you get it back."""
    batch = await db.get(ImportBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Batch not found")
    return StreamingResponse(
        io.BytesIO(batch.file_bytes),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{batch.file_name}"'},
    )


@router.get("/{batch_id}/result-report")
async def download_result_report(
    batch_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("import.batch.view")),
) -> StreamingResponse:
    """A downloadable result report showing exactly what was applied, skipped and
    rejected, with row numbers (§10)."""
    batch = await import_service.get_batch_with_rows(db, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Batch not found")

    wb = Workbook()
    ws = wb.active
    ws.title = "Import Result"
    ws.append(["Row", "Action", "Errors", "Parsed Values"])
    for row in batch.rows:
        ws.append([row.line_number, row.computed_action, "; ".join(row.validation_messages), str(row.parsed_values)])

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="import_result_{batch_id}.xlsx"'},
    )
