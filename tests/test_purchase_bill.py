"""GST on a vendor purchase bill. No database needed."""

from decimal import Decimal

from app.services.purchase_bill import build_bill, line_amounts, stock_in


def line(qty, cost, rate):
    return {"quantity": qty, "unit_cost": cost, "tax_rate": rate}


def test_within_the_state_tax_is_cgst_plus_sgst():
    bill = build_bill([line(10, 100, 18)], inter_state=False)
    assert bill["lines"][0]["taxable"] == 1000
    assert (bill["lines"][0]["cgst"], bill["lines"][0]["sgst"], bill["lines"][0]["igst"]) == (90, 90, 0)
    assert bill["totals"] == {
        "taxable": 1000, "cgst": 90, "sgst": 90, "igst": 0, "gst": 180, "discount": 0, "cess": 0, "surcharge": 0, "round_off": 0, "invoice_total": 1180,
    }


def test_between_states_it_is_igst():
    bill = build_bill([line(10, 100, 18)], inter_state=True)
    assert (bill["totals"]["cgst"], bill["totals"]["sgst"], bill["totals"]["igst"]) == (0, 0, 180)
    assert bill["inter_state"] is True


def test_the_summary_groups_lines_by_gst_rate():
    bill = build_bill([line(2, 50, 5), line(1, 200, 18), line(4, 25, 5)], inter_state=False)
    assert [(r["rate"], r["taxable"]) for r in bill["by_rate"]] == [(5, 200), (18, 200)]
    assert bill["by_rate"][0]["cgst"] == 5 and bill["by_rate"][0]["sgst"] == 5


def test_the_two_halves_always_add_back_to_the_tax():
    # 3 x 1.01 = 3.03 taxable; 5% = 0.1515 -> 0.15; halves 0.08 + 0.07
    bill = build_bill([line(3, 1.01, 5)], inter_state=False)
    ln = bill["lines"][0]
    assert ln["gst"] == 0.15
    assert round(ln["cgst"] + ln["sgst"], 2) == 0.15


def test_the_invoice_total_is_rounded_to_the_rupee_with_the_difference_shown():
    bill = build_bill([line(1, 99.5, 12)], inter_state=False)  # 99.50 + 11.94 = 111.44
    assert bill["totals"]["invoice_total"] == 111
    assert bill["totals"]["round_off"] == -0.44


def test_fractional_quantities_work():
    bill = build_bill([line(2.5, 40, 0)], inter_state=False)
    assert bill["totals"]["taxable"] == 100
    assert bill["totals"]["gst"] == 0 and bill["totals"]["invoice_total"] == 100


# ---- the vendor-bill grid: free / reject / breakage quantity, discount, GST code, cess, surcharge

def test_a_line_works_out_like_the_vendors_grid():
    # 10 x 54.880 = 548.80, GST 5% = 27.44, net 576.24
    a = line_amounts(receipt=10, rate=54.880, gst_rate=5)
    assert (a["amount"], a["gst"], a["net"]) == (Decimal("548.80"), Decimal("27.44"), Decimal("576.24"))
    # 10 x 62.566 = 625.66, GST 5% = 31.28, net 656.94
    b = line_amounts(receipt=10, rate=62.566, gst_rate=5)
    assert (b["amount"], b["gst"], b["net"]) == (Decimal("625.66"), Decimal("31.28"), Decimal("656.94"))


def test_discount_comes_off_before_gst_and_cess_and_surcharge_go_on():
    a = line_amounts(receipt=10, rate=100, discount=100, gst_rate=18, cess=7, surcharge_rate=2)
    # amount 1000, taxable 900, GST 162, surcharge 18, cess 7
    assert (a["amount"], a["taxable"], a["gst"], a["surcharge"], a["cess"], a["net"]) == (1000, 900, 162, 18, 7, 1087)  # Decimal == int is fine


def test_free_goods_cost_nothing_but_go_into_stock():
    assert stock_in(10, free=2) == 12
    assert line_amounts(receipt=10, rate=50, gst_rate=0)["amount"] == 500  # the free 2 are not charged


def test_rejected_and_broken_goods_never_enter_stock():
    assert stock_in(10, free=2, reject=3, hl=1) == 8
    assert stock_in(10, reject=10) == 0


def test_the_bill_totals_include_discount_cess_and_surcharge():
    ln = {"quantity": 10, "unit_cost": 100, "tax_rate": 18, "discount": 100, "cess": 7, "surcharge_rate": 2, "free_qty": 2, "reject_qty": 0, "hl_qty": 1}
    bill = build_bill([ln], inter_state=False)
    t = bill["totals"]
    assert (t["taxable"], t["gst"], t["discount"], t["cess"], t["surcharge"]) == (900, 162, 100, 7, 18)
    assert t["invoice_total"] == 1087 and t["round_off"] == 0
    assert bill["lines"][0]["stock_in"] == 11 and bill["lines"][0]["total"] == 1087


def test_a_line_saved_before_these_columns_read_exactly_as_before():
    old = build_bill([line(10, 100, 18)], inter_state=False)["lines"][0]
    assert old["taxable"] == 1000 and old["total"] == 1180 and old["cess"] == 0 and old["surcharge"] == 0
