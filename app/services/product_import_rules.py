"""Pure (database-free) rules for the product-master Excel import: how a row is
read, checked, and compared with the product already on file. Kept separate so
it can be unit-tested without a database.

A blank cell never changes anything: on an existing product only the columns
the sheet actually fills in are compared and updated.
"""

TEXT_FIELDS = ("name", "barcode", "uom", "hsn_code")
NUMBER_FIELDS = ("purchase_price", "selling_price", "mrp", "tax_rate")
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


def header_key(raw) -> str:
    return str(raw or "").strip().lower().replace(" ", "_")


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
            out.append(f"{LABELS[f]}: {c['old'] if c['old'] is not None else '—'} → {c['new']}")
    return out
