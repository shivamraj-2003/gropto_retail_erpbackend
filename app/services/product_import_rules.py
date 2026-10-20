"""Pure (database-free) rules for the product-master Excel import: how a row is
read, checked, and compared with the product already on file. Kept separate so
it can be unit-tested without a database.

A blank cell never changes anything: on an existing product only the columns
the sheet actually fills in are compared and updated.
"""

import re

TEXT_FIELDS = ("name", "barcode", "uom", "hsn_code", "brand", "category")
NUMBER_FIELDS = ("purchase_price", "selling_price", "mrp", "tax_rate", "pack_size")
# Customer-facing prices: changing them goes through approval (one request for the whole file).
PRICE_FIELDS = ("selling_price", "mrp")
LABELS = {
    "name": "Name",
    "barcode": "Barcode",
    "uom": "Unit",
    "hsn_code": "HSN code",
    "purchase_price": "Purchase price",
    "selling_price": "Selling price",
    "mrp": "MRP",
    "tax_rate": "GST %",
    "brand": "Brand",
    "category": "Category",
    "pack_size": "Pack size",
    "other_barcodes": "Other barcodes",
}


def _blank(raw) -> bool:
    return raw is None or str(raw).strip() == ""


def clean_text(raw) -> str | None:
    """Excel hands numeric-looking cells (barcodes, HSN) back as floats — turn 8901000000001.0 back into '8901000000001'."""
    if _blank(raw):
        return None
    if isinstance(raw, float) and raw.is_integer():
        return str(int(raw))
    return str(raw).strip()


def split_codes(raw) -> list[str]:
    """'111, 222;333' -> ['111', '222', '333'] (Excel numbers cleaned, repeats dropped)."""
    if _blank(raw):
        return []
    if isinstance(raw, (int, float)):
        parts = [clean_text(raw) or ""]
    else:
        parts = str(raw).replace(";", ",").replace("\n", ",").split(",")
    out: list[str] = []
    for part in parts:
        code = part.strip()
        if code and code not in out:
            out.append(code)
    return out


# Column names people actually use, mapped to the ones the importer understands, so a sheet exported from another
# billing tool or typed by hand works without renaming its headings. Unknown columns are ignored.
PRODUCT_ALIASES = {
    "sku": ("item_code", "product_code", "code", "sku_code", "item_sku", "article_no", "article_code"),
    "name": ("item_name", "product_name", "product", "item", "description", "item_description"),
    "barcode": ("bar_code", "ean", "upc", "barcode_no", "primary_barcode", "main_barcode"),
    "other_barcodes": ("alt_barcodes", "additional_barcodes", "extra_barcodes", "other_barcode", "alternate_barcodes"),
    "uom": ("unit", "units", "unit_of_measure", "uom_code"),
    "purchase_price": ("cost", "cost_price", "purchase_rate", "buying_price", "purchase_cost", "cp"),
    "selling_price": ("sale_price", "selling_rate", "sp", "price", "rate", "sales_price", "retail_price"),
    "mrp": ("max_retail_price", "mrp_price"),
    "tax_rate": ("gst", "gst_rate", "tax", "gst_percent", "tax_percent", "gst_pct", "tax_pct"),
    "hsn_code": ("hsn", "hsn_sac", "hsn_no"),
    "brand": ("brand_name", "company", "manufacturer"),
    "category": ("category_name", "group", "department", "item_group"),
    "pack_size": ("pack", "pack_qty", "pack_weight"),
    "quantity": ("qty", "stock", "opening_stock", "stock_qty", "closing_stock", "on_hand"),
    "batch_number": ("batch", "batch_no", "batch_num"),
    "mfg_date": ("mfg", "manufacturing_date", "manufactured_on", "mfd"),
    "expiry_date": ("expiry", "exp_date", "exp", "expiry_on", "expires_on"),
    "label": ("price_name", "batch_name"),
}
CUSTOMER_ALIASES = {
    "phone": ("mobile", "mobile_no", "mobile_number", "phone_number", "contact_no", "contact_number", "whatsapp", "phone_no"),
    "name": ("customer_name", "full_name", "customer"),
    "email": ("email_id", "e_mail", "mail", "email_address"),
}
VENDOR_ALIASES = {
    "name": ("vendor_name", "supplier", "supplier_name", "vendor", "company_name"),
    "gst_number": ("gstin", "gst_no", "gst", "gst_number", "gst_id"),
    "phone": ("mobile", "mobile_no", "phone_number", "contact_no", "contact_number", "phone_no"),
    "email": ("email_id", "e_mail", "mail", "email_address"),
    "contact_person": ("contact_name", "contact", "person"),
    "credit_days": ("credit_period", "payment_terms", "credit_term", "credit"),
    "address_line1": ("address", "address1", "address_1", "street"),
    "address_line2": ("address2", "address_2"),
    "pan_number": ("pan", "pan_no"),
    "pincode": ("pin", "pin_code", "zip", "postal_code"),
    "legal_name": ("registered_name",),
    "bank_account_number": ("account_number", "account_no", "bank_account"),
    "bank_ifsc": ("ifsc", "ifsc_code"),
    "bank_name": ("bank",),
    "alt_phone": ("alternate_phone", "alt_mobile"),
}


def header_key(raw, aliases: dict | None = None) -> str:
    """'Selling Price', 'selling-price' and 'SELLING_PRICE' are the same column; with `aliases`, so are 'MRP price',
    'Cost' (purchase_price) and the other common names."""
    key = re.sub(r"[^a-z0-9]+", "_", str(raw or "").strip().lower()).strip("_")
    for canonical, names in (aliases or {}).items():
        if key == canonical or key in names:
            return canonical
    return key


def parse_product_row(values: dict, *, is_new: bool) -> tuple[dict, list[str]]:
    """Returns (fields the sheet provided, problems found). `values` keys are header names."""
    messages: list[str] = []
    out: dict = {}

    for field in TEXT_FIELDS:
        text = clean_text(values.get(field))
        if text is not None:
            out[field] = text

    for field in NUMBER_FIELDS:
        raw = values.get(field)
        if _blank(raw):
            continue
        try:
            number = float(raw)
        except (TypeError, ValueError):
            messages.append(f"{LABELS[field]} is not a number")
            continue
        if number < 0:
            messages.append(f"{LABELS[field]} cannot be negative")
            continue
        if field == "pack_size" and number <= 0:
            messages.append("Pack size must be more than 0")
            continue
        if field == "tax_rate" and number > 100:
            messages.append("GST % must be between 0 and 100")
            continue
        out[field] = round(number, 2)

    others = split_codes(values.get("other_barcodes"))
    if others:
        out["other_barcodes"] = others

    if is_new:
        if "name" not in out:
            messages.append("Name is required for a new product")
        if "selling_price" not in out:
            messages.append("Selling price is required for a new product")
        elif out.get("mrp") is not None and out["mrp"] < out["selling_price"]:
            messages.append("MRP is lower than the selling price")
    return out, messages


def diff_product(existing: dict, provided: dict) -> dict[str, dict]:
    """{field: {"old": ..., "new": ...}} for every provided value that differs from what is on file."""
    changes: dict[str, dict] = {}
    for field, new in provided.items():
        old = existing.get(field)
        if field == "other_barcodes":
            known = set(existing.get("other_barcodes") or []) | ({existing["barcode"]} if existing.get("barcode") else set())
            added = [c for c in new if c not in known]
            if added:
                changes[field] = {"old": existing.get("other_barcodes") or [], "new": added}
        elif field in NUMBER_FIELDS:
            if old is None or abs(float(old) - float(new)) >= 0.005:
                changes[field] = {"old": None if old is None else float(old), "new": float(new)}
        elif (old or None) != new:
            changes[field] = {"old": old, "new": new}
    return changes


def describe_changes(changes: dict[str, dict]) -> list[str]:
    out = []
    for f, c in changes.items():
        if f == "other_barcodes":
            out.append(f"Other barcodes: add {', '.join(c['new'])}")
        else:
            out.append(f"{LABELS.get(f, f.replace('_', ' ').capitalize())}: {c['old'] if c['old'] not in (None, '') else '—'} → {c['new']}")
    return out
