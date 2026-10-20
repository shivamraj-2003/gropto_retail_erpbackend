"""DB-free checks of the product-import row rules."""

from app.services.product_import_rules import clean_text, describe_changes, diff_product, header_key, parse_product_row

EXISTING = {
    "name": "Bournvita 500g", "barcode": "8901233012345", "uom": "EA", "hsn_code": "1806",
    "purchase_price": 215.0, "selling_price": 240.0, "mrp": 250.0, "tax_rate": 18.0,
}


def test_excel_numbers_become_clean_text():
    assert clean_text(8901233012345.0) == "8901233012345"
    assert clean_text("  SKU-1 ") == "SKU-1"
    assert clean_text("") is None and clean_text(None) is None


def test_header_names_are_forgiving():
    assert header_key(" Selling Price ") == "selling_price"


def test_new_product_needs_name_and_selling_price():
    _, msgs = parse_product_row({"sku": "X"}, is_new=True)
    assert "Name is required for a new product" in msgs and "Selling price is required for a new product" in msgs
    fields, msgs = parse_product_row({"sku": "X", "name": "Tea", "selling_price": 50}, is_new=True)
    assert not msgs and fields["selling_price"] == 50.0


def test_bad_numbers_are_reported_not_crashed():
    _, msgs = parse_product_row({"name": "A", "selling_price": "abc", "tax_rate": 150, "purchase_price": -1}, is_new=False)
    assert any("Selling price is not a number" in m for m in msgs)
    assert any("GST %" in m for m in msgs)
    assert any("Purchase price cannot be negative" in m for m in msgs)


def test_mrp_below_selling_price_flagged_for_new_product():
    _, msgs = parse_product_row({"name": "A", "selling_price": 100, "mrp": 90}, is_new=True)
    assert "MRP is lower than the selling price" in msgs


def test_blank_cells_leave_existing_values_alone():
    provided, msgs = parse_product_row({"sku": "S", "name": "", "selling_price": "", "tax_rate": 18}, is_new=False)
    assert not msgs and provided == {"tax_rate": 18.0}
    assert diff_product(EXISTING, provided) == {}


def test_diff_reports_only_real_changes():
    provided, _ = parse_product_row({"name": "Bournvita 500g", "selling_price": 245, "mrp": 250, "barcode": "111"}, is_new=False)
    changes = diff_product(EXISTING, provided)
    assert set(changes) == {"selling_price", "barcode"}
    assert changes["selling_price"] == {"old": 240.0, "new": 245.0}
    assert "Selling price: 240.0 → 245.0" in describe_changes(changes)


def test_tiny_rounding_is_not_a_change():
    assert diff_product(EXISTING, {"selling_price": 240.004}) == {}


def test_other_barcodes_are_split_and_diffed():
    from app.services.product_import_rules import split_codes

    assert split_codes("111, 222;333,111") == ["111", "222", "333"]
    assert split_codes(8901.0) == ["8901"]
    provided, msgs = parse_product_row({"other_barcodes": "111, 222"}, is_new=False)
    assert not msgs and provided["other_barcodes"] == ["111", "222"]
    existing = {**EXISTING, "other_barcodes": ["111"]}
    changes = diff_product(existing, provided)
    assert changes == {"other_barcodes": {"old": ["111"], "new": ["222"]}}
    assert describe_changes(changes) == ["Other barcodes: add 222"]
    # the main barcode listed again among the others is not a change
    assert diff_product(existing, {"other_barcodes": ["8901233012345"]}) == {}


def test_common_column_names_are_understood():
    from app.services.product_import_rules import CUSTOMER_ALIASES, PRODUCT_ALIASES, VENDOR_ALIASES

    assert header_key("Item Name", PRODUCT_ALIASES) == "name"
    assert header_key("GST %", PRODUCT_ALIASES) == "tax_rate"
    assert header_key("Cost", PRODUCT_ALIASES) == "purchase_price"
    assert header_key("Exp. Date", PRODUCT_ALIASES) == "expiry_date"
    assert header_key("Mobile No", CUSTOMER_ALIASES) == "phone"
    assert header_key("GSTIN", VENDOR_ALIASES) == "gst_number"
    assert header_key("Some Random Column", PRODUCT_ALIASES) == "some_random_column"


def test_brand_category_and_pack_size_are_read():
    fields, msgs = parse_product_row({"name": "Tea", "selling_price": 50, "brand": "Tata", "category": "Staples", "pack_size": 250}, is_new=True)
    assert not msgs and fields["brand"] == "Tata" and fields["category"] == "Staples" and fields["pack_size"] == 250.0
    _, msgs = parse_product_row({"name": "Tea", "selling_price": 50, "pack_size": 0}, is_new=True)
    assert "Pack size must be more than 0" in msgs
    assert diff_product({"category": "Staples", "pack_size": 1.0}, {"category": "Snacks"}) == {"category": {"old": "Staples", "new": "Snacks"}}


def test_phone_numbers_are_cleaned():
    from app.services.imports import _clean_phone

    assert _clean_phone(9876543210.0) == "9876543210"
    assert _clean_phone("+91 98765-43210") == "9876543210"
    assert _clean_phone("") is None
