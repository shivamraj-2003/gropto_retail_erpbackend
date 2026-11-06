"""What a purchase at a different MRP does to prices. No database needed."""

from app.services.purchase_prices import default_selling_price, mrp_differs, suggested_action, units_at_current_price


def test_only_a_real_difference_in_mrp_counts():
    assert mrp_differs(110, 100)
    assert not mrp_differs(100, 100)
    assert not mrp_differs(100.004, 100)
    assert not mrp_differs(None, 100)


def test_the_new_selling_price_keeps_the_products_usual_discount():
    # sold at 95 on an MRP of 100 (5% off): a new MRP of 120 sells at 114
    assert default_selling_price(100, 95, 120) == 114
    assert default_selling_price(100, 100, 120) == 120  # sold at MRP stays at MRP
    assert default_selling_price(0, 0, 50) == 50  # no old price to copy


def test_the_selling_price_is_never_above_the_mrp():
    assert default_selling_price(100, 120, 110) == 110


def test_a_higher_mrp_is_a_price_rise_and_a_lower_one_is_a_one_off_lot():
    assert suggested_action(120, 100) == "new_price"
    assert suggested_action(90, 100) == "new_stock_price"


def test_units_at_the_current_price_are_the_total_less_the_older_price_shares():
    assert units_at_current_price(30, [10, 5]) == 15
    assert units_at_current_price(8, [18]) == 0  # shares can exceed a stock that has since fallen
    assert units_at_current_price(0, []) == 0
    assert units_at_current_price(12, []) == 12
