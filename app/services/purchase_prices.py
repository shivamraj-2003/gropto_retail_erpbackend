"""What a purchase line does to a product's prices when its MRP is not the product's MRP. Pure rules, no database.

A product has one CURRENT price (MRP and selling price) and, besides it, older prices still on the shelf ("other prices", each
with the number of units a store holds at it). Two situations follow when a bill arrives at a different MRP:

  new_price        the new MRP becomes the product's current price; what the store already holds keeps the OLD price as an
                   "other price" (the usual price rise).
  new_stock_price  only the units on this bill carry the new MRP, as an "other price"; the product's current price stays.

Both are price changes, so they go through the approval engine like any other (a Super Admin's apply at once).
"""

MRP_ACTIONS = ("keep", "new_price", "new_stock_price")


def mrp_differs(line_mrp: float | None, product_mrp: float) -> bool:
    return line_mrp is not None and abs(float(line_mrp) - float(product_mrp)) >= 0.005


def default_selling_price(old_mrp: float, old_selling: float, new_mrp: float) -> float:
    """The selling price at a new MRP that keeps the product's usual discount. Never above the MRP."""
    if old_mrp <= 0:
        return round(float(new_mrp), 2)
    return round(min(float(new_mrp), float(new_mrp) * float(old_selling) / float(old_mrp)), 2)


def suggested_action(line_mrp: float, product_mrp: float) -> str:
    """A higher MRP is normally a price rise (the new stock sets the price); a lower one is a one-off lot."""
    return "new_price" if float(line_mrp) > float(product_mrp) else "new_stock_price"


def units_at_current_price(store_total: float, variant_shares: list[float]) -> float:
    """What the store holds at the product's current price: the total less the units kept at older prices."""
    return round(max(float(store_total) - sum(max(float(s), 0.0) for s in variant_shares), 0.0), 3)
