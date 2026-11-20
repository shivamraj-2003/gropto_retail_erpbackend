"""GSTR-1 / GSTR-3B arithmetic. No database needed."""

import pytest

from app.services.gst_returns import _note_amounts, month_bounds, set_off, state_code


def test_a_month_runs_to_the_first_of_the_next():
    assert [d.isoformat() for d in month_bounds("2026-10")] == ["2026-10-01", "2026-11-01"]
    assert [d.isoformat() for d in month_bounds("2026-12")] == ["2026-12-01", "2027-01-01"]
    with pytest.raises(ValueError):
        month_bounds("2026-13")


def test_state_names_become_gst_state_codes():
    assert state_code("Uttar Pradesh") == "09"
    assert state_code("  uttar pradesh ") == "09"
    assert state_code("Nowhere") == ""
    assert state_code(None) == ""


def test_a_refund_is_split_into_taxable_value_and_tax_the_way_the_sale_was():
    intra = _note_amounts(118.0, 18, inter_state=False)
    assert intra == {"txval": 100.0, "igst": 0.0, "cgst": 9.0, "sgst": 9.0}
    inter = _note_amounts(118.0, 18, inter_state=True)
    assert inter == {"txval": 100.0, "igst": 18.0, "cgst": 0.0, "sgst": 0.0}
    assert _note_amounts(50.0, 0, inter_state=False) == {"txval": 50.0, "igst": 0.0, "cgst": 0.0, "sgst": 0.0}


def test_the_two_halves_of_a_refund_tax_add_back_to_the_tax():
    n = _note_amounts(10.0, 5, inter_state=False)  # taxable 9.52, tax 0.48
    assert round(n["cgst"] + n["sgst"], 2) == round(10.0 - n["txval"], 2)


def test_credit_pays_tax_in_the_order_the_rules_give():
    # CGST credit pays CGST then IGST; it never pays SGST
    r = set_off({"igst": 50, "cgst": 100, "sgst": 100}, {"igst": 0, "cgst": 160, "sgst": 0})
    assert r["payable"] == {"igst": 0.0, "cgst": 0.0, "sgst": 100.0} and r["carried_forward"] == {"igst": 0.0, "cgst": 10.0, "sgst": 0.0}


def test_igst_credit_pays_igst_then_cgst_then_sgst():
    r = set_off({"igst": 10, "cgst": 20, "sgst": 30}, {"igst": 45, "cgst": 0, "sgst": 0})
    assert r["payable"] == {"igst": 0.0, "cgst": 0.0, "sgst": 15.0} and r["carried_forward"]["igst"] == 0.0


def test_credit_left_over_is_carried_forward_not_lost():
    r = set_off({"igst": 0, "cgst": 10, "sgst": 10}, {"igst": 0, "cgst": 25, "sgst": 5})
    assert r["payable"] == {"igst": 0.0, "cgst": 0.0, "sgst": 5.0}
    assert r["carried_forward"] == {"igst": 0.0, "cgst": 15.0, "sgst": 0.0}


def test_no_credit_means_the_full_tax_is_payable():
    r = set_off({"igst": 1, "cgst": 2, "sgst": 3}, {})
    assert r["payable"] == {"igst": 1.0, "cgst": 2.0, "sgst": 3.0}
