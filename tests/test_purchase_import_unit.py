"""The purchase-bill file: how its headings and numbers are read. No database needed."""

from app.services.imports import PURCHASE_BILL_ALIASES, _number, _rows_of


def test_the_vendors_own_headings_are_understood():
    csv = "Item Code,Bill Serial,MRP,Batch No.,HSN,Qty,Free,Reject,H.L.,Rate,Disc,GST %,Add. Cess,Surcharge\nA1,1,10,B1,0910,10,1,0,0,6.857,5,5,0,0\n"
    header, rows = _rows_of(csv.encode(), PURCHASE_BILL_ALIASES)
    assert header == ["sku", "bill_serial", "mrp", "batch_no", "hsn_code", "receipt", "free", "reject", "hl", "rate", "discount", "gst", "cess", "surcharge_rate"]
    assert len(rows) == 1


def test_our_own_template_headings_are_understood():
    csv = "sku,bill_serial,mrp,batch_no,hsn_code,receipt,free,reject,hl,rate,discount,gst,cess,surcharge_rate\nA1,1,10,B1,0910,10,,,,6.857,,5,,\n"
    header, _ = _rows_of(csv.encode(), PURCHASE_BILL_ALIASES)
    assert header[:3] == ["sku", "bill_serial", "mrp"] and "receipt" in header and "rate" in header and "surcharge_rate" in header


def test_numbers_are_read_with_commas_and_percent_signs_and_blank_means_the_default():
    msgs: list[str] = []
    assert _number("1,250.5", "rate", msgs, default=None) == 1250.5
    assert _number("5%", "gst", msgs, default=None) == 5
    assert _number("", "free", msgs) == 0
    assert _number(None, "rate", msgs, default=None) is None
    assert msgs == []


def test_a_bad_or_negative_number_is_reported_not_guessed():
    msgs: list[str] = []
    _number("abc", "rate", msgs)
    _number("-3", "discount", msgs)
    assert len(msgs) == 2 and "rate" in msgs[0] and "discount" in msgs[1]
