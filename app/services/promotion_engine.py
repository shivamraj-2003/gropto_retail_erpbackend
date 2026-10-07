"""BOGO / combo / category-percent promotion evaluation (Point 9 audit fix).

PromotionRule existed as a pure CRUD island — zero evaluation logic anywhere,
no way for a BOGO or combo rule to ever actually discount a cart. This gives
it a real (intentionally conservative) evaluator plus a per-firing redemption
log, wired into OMS order creation where the server already owns the full
cart server-side after the Point 9 pricing fix.

Rule shape (conditions_json / discount_json), by promo_type:

  bogo:
    conditions_json: {"product_id": "<uuid>", "buy_qty": 1}
    discount_json:   {"get_qty": 1, "discount_percent": 100}
    Every (buy_qty + get_qty) units of product_id in the cart gets get_qty
    units discounted by discount_percent (default 100 = free).

  combo:
    conditions_json: {"product_ids": ["<uuid>", ...]}
    discount_json:   {"combo_price": 199.0}  OR  {"discount_percent": 10}
    Fires once per complete set of all listed products present (min qty 1
    each) in the cart. combo_price caps the combined line value of exactly
    one unit of each product; discount_percent is a simple percent off that
    same combined value if combo_price isn't set.

  category_percent:
    conditions_json: {"category_id": "<uuid>"}
    discount_json:   {"percent": 10}
    Percent off every cart line whose product belongs to that category.

Deliberately NOT implemented here: stacking across multiple rules on the
same line (first matching rule per type wins, in rule-list order) and
cross-rule priority configuration — the spec's "promotion priority" section
has no priority field anywhere on PromotionRule to drive that from.
"""

import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.models import Product
from app.models.models_phase4 import PromotionRedemption, PromotionRule


class CartLine:
    __slots__ = ("product_id", "category_id", "quantity", "unit_price", "discount")

    def __init__(self, product_id: uuid.UUID, category_id: uuid.UUID | None, quantity: float, unit_price: float):
        self.product_id = product_id
        self.category_id = category_id
        self.quantity = quantity
        self.unit_price = unit_price
        self.discount = 0.0


async def _active_rules(db: AsyncSession) -> list[PromotionRule]:
    return list((await db.execute(select(PromotionRule).where(PromotionRule.active.is_(True)))).scalars().all())


def _apply_bogo(rule: PromotionRule, lines: list[CartLine]) -> float:
    cond = rule.conditions_json or {}
    disc = rule.discount_json or {}
    try:
        product_id = uuid.UUID(str(cond["product_id"]))
    except (KeyError, ValueError, TypeError):
        return 0.0
    buy_qty = max(float(cond.get("buy_qty", 1)), 1)
    get_qty = max(float(disc.get("get_qty", 1)), 0)
    percent = float(disc.get("discount_percent", 100))
    total_discount = 0.0
    for line in lines:
        if line.product_id != product_id or get_qty <= 0:
            continue
        set_size = buy_qty + get_qty
        sets = int(line.quantity // set_size)
        if sets <= 0:
            continue
        free_units = sets * get_qty
        line_discount = round(free_units * line.unit_price * percent / 100, 2)
        line.discount += line_discount
        total_discount += line_discount
    return total_discount


def _apply_combo(rule: PromotionRule, lines: list[CartLine]) -> float:
    cond = rule.conditions_json or {}
    disc = rule.discount_json or {}
    try:
        product_ids = [uuid.UUID(str(p)) for p in cond.get("product_ids", [])]
    except (ValueError, TypeError):
        return 0.0
    if len(product_ids) < 2:
        return 0.0
    by_product = {line.product_id: line for line in lines}
    if not all(pid in by_product and by_product[pid].quantity >= 1 for pid in product_ids):
        return 0.0

    combo_lines = [by_product[pid] for pid in product_ids]
    combined_value = sum(line.unit_price for line in combo_lines)  # one unit of each

    if "combo_price" in disc:
        combo_price = float(disc["combo_price"])
        discount = max(combined_value - combo_price, 0.0)
    else:
        percent = float(disc.get("discount_percent", 0))
        discount = round(combined_value * percent / 100, 2)
    if discount <= 0:
        return 0.0

    # Spread the discount across the component lines proportional to value,
    # so per-line discount is meaningful (e.g. for margin reporting).
    for line in combo_lines:
        share = (line.unit_price / combined_value) * discount if combined_value else 0.0
        line.discount += round(share, 2)
    return round(discount, 2)


def _apply_category_percent(rule: PromotionRule, lines: list[CartLine]) -> float:
    cond = rule.conditions_json or {}
    disc = rule.discount_json or {}
    try:
        category_id = uuid.UUID(str(cond["category_id"]))
    except (KeyError, ValueError, TypeError):
        return 0.0
    percent = float(disc.get("percent", 0))
    if percent <= 0:
        return 0.0
    total_discount = 0.0
    for line in lines:
        if line.category_id != category_id:
            continue
        line_discount = round(line.quantity * line.unit_price * percent / 100, 2)
        line.discount += line_discount
        total_discount += line_discount
    return total_discount


_EVALUATORS = {
    "bogo": _apply_bogo,
    "combo": _apply_combo,
    "category_percent": _apply_category_percent,
}


async def evaluate_preview(db: AsyncSession, *, lines: list[CartLine]) -> list[tuple[PromotionRule, float]]:
    """Same evaluation as evaluate_and_log but writes nothing: returns each
    rule that fires with its discount, for the till's live cart preview."""
    fired: list[tuple[PromotionRule, float]] = []
    for rule in await _active_rules(db):
        evaluator = _EVALUATORS.get(rule.promo_type)
        if evaluator is None:
            continue
        amount = evaluator(rule, lines)
        if amount > 0:
            fired.append((rule, round(amount, 2)))
    return fired


async def evaluate_and_log(
    db: AsyncSession,
    *,
    store_id: uuid.UUID | None,
    lines: list[CartLine],
    source_type: str,
    source_id: uuid.UUID,
) -> float:
    """Evaluates every active PromotionRule against the cart, mutates each
    CartLine's .discount in place, writes one PromotionRedemption per rule
    that actually fired (idempotent under replay), and returns the total
    discount amount."""
    rules = await _active_rules(db)
    total_discount = 0.0
    for rule in rules:
        evaluator = _EVALUATORS.get(rule.promo_type)
        if evaluator is None:
            continue
        fired_discount = evaluator(rule, lines)
        if fired_discount <= 0:
            continue
        try:
            async with db.begin_nested():
                db.add(
                    PromotionRedemption(
                        promotion_rule_id=rule.id,
                        source_type=source_type,
                        source_id=source_id,
                        store_id=store_id,
                        discount_amount=round(fired_discount, 2),
                        details_json={"promo_type": rule.promo_type, "name": rule.name},
                    )
                )
                await db.flush()
        except IntegrityError:
            # Already logged for this source (replay) — scoped rollback via
            # the savepoint above, the outer transaction is untouched.
            continue
        total_discount += fired_discount
    return round(total_discount, 2)


async def build_lines(db: AsyncSession, *, items: list[dict]) -> list[CartLine]:
    """items: dicts with product_id/quantity/unit_price already resolved."""
    product_ids = [item["product_id"] for item in items]
    products = {
        p.id: p
        for p in (await db.execute(select(Product).where(Product.id.in_(product_ids)))).scalars().all()
    }
    lines = []
    for item in items:
        product = products.get(item["product_id"])
        lines.append(
            CartLine(
                product_id=item["product_id"],
                category_id=product.category_id if product else None,
                quantity=float(item["quantity"]),
                unit_price=float(item["unit_price"]),
            )
        )
    return lines
