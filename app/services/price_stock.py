"""Stock of one product held at more than one price.

The store's total stays the one real stock number (sales, transfers and counts all move it). The units that still
carry an extra, older price are a share of that total, kept per variant and store. What is left over is at the
product's current price. Pure functions; the database work lives in the callers.
"""


def split_stock(total: float, variant_qtys: list[tuple[str, float]]) -> tuple[float, dict[str, float]]:
    """Returns (units at the current price, units per variant id). A variant share can never push the total over what the
    store holds (stock may have left by transfer or count since the share was set), so shares are cut back in the order
    given, and the current price gets the rest."""
    left = max(float(total), 0.0)
    shares: dict[str, float] = {}
    for variant_id, qty in variant_qtys:
        take = min(max(float(qty), 0.0), left)
        shares[variant_id] = round(take, 3)
        left -= take
    return round(left, 3), shares


def match_variant(variants: list[dict], mrp: float | None, selling_price: float) -> str | None:
    """The variant a sold line belongs to: same MRP and same selling price as an active variant. A line at the product's
    current price matches none."""
    if mrp is None:
        return None
    for v in variants:
        if abs(float(v["mrp"]) - float(mrp)) < 0.005 and abs(float(v["selling_price"]) - float(selling_price)) < 0.005:
            return str(v["id"])
    return None


def after_sale(current_qty: float, sold: float) -> float:
    """A variant's share after selling `sold` of it. It stops at zero: selling more than the share just means the pack
    count was off, and the store total (not this share) is what records negative stock."""
    return round(max(float(current_qty) - float(sold), 0.0), 3)
