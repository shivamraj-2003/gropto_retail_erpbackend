"""Stock at more than one price. No database needed."""

from app.services.price_stock import after_sale, match_variant, split_stock


def test_the_current_price_gets_what_the_old_price_does_not():
    current, shares = split_stock(30, [("a", 18)])
    assert current == 12 and shares == {"a": 18}


def test_two_old_prices_come_off_the_total():
    current, shares = split_stock(30, [("a", 10), ("b", 5)])
    assert current == 15 and shares == {"a": 10, "b": 5}


def test_shares_never_exceed_what_the_store_holds():
    current, shares = split_stock(8, [("a", 18)])
    assert current == 0 and shares == {"a": 8}


def test_no_stock_means_nothing_anywhere():
    current, shares = split_stock(0, [("a", 4)])
    assert current == 0 and shares == {"a": 0}
    assert split_stock(-3, []) == (0, {})


def test_a_sold_line_matches_the_variant_with_its_mrp_and_price():
    variants = [{"id": "a", "mrp": 100, "selling_price": 95}]
    assert match_variant(variants, 100, 95) == "a"
    assert match_variant(variants, 110, 105) is None  # the current price
    assert match_variant(variants, None, 95) is None  # old bills carry no MRP


def test_a_share_stops_at_zero():
    assert after_sale(5, 2) == 3
    assert after_sale(2, 5) == 0


def test_the_split_shown_on_the_product_list():
    import uuid
    from types import SimpleNamespace

    from app.api.v1.products import _price_split

    va, vb = uuid.uuid4(), uuid.uuid4()
    product = SimpleNamespace(
        mrp=110, selling_price=105,
        price_variants=[
            SimpleNamespace(id=va, is_active=True, label="Old stock", mrp=100, selling_price=95),
            SimpleNamespace(id=vb, is_active=False, label="Gone", mrp=90, selling_price=85),
        ],
    )
    rows = _price_split(product, 30, {va: 18, vb: 5})
    assert [(r["label"], r["quantity"]) for r in rows] == [("Current price", 12), ("Old stock", 18)]
