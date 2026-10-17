"""GST on a vendor purchase bill. No database needed."""

from app.services.purchase_bill import build_bill


def line(qty, cost, rate):
    return {"quantity": qty, "unit_cost": cost, "tax_rate": rate}


def test_within_the_state_tax_is_cgst_plus_sgst():
    bill = build_bill([line(10, 100, 18)], inter_state=False)
    assert bill["lines"][0]["taxable"] == 1000
    assert (bill["lines"][0]["cgst"], bill["lines"][0]["sgst"], bill["lines"][0]["igst"]) == (90, 90, 0)
    assert bill["totals"] == {"taxable": 1000, "cgst": 90, "sgst": 90, "igst": 0, "gst": 180, "round_off": 0, "invoice_total": 1180}


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
