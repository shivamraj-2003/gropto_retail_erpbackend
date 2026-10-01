"""Central/store/city price resolution (Point 9 audit fix).

StorePriceOverride existed as a pure CRUD island — created, listed, never
consulted by POS or OMS at the moment of sale. This gives it a real
precedence resolver: a store-specific override wins over a city-specific
override, which wins over the central Product.selling_price/mrp. Channel-
specific pricing isn't modelled anywhere in the schema (no channel column on
StorePriceOverride) so it isn't part of this precedence chain.
"""

import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import Product, Store
from app.models.models_phase4 import StorePriceOverride


async def resolve_price(db: AsyncSession, *, product: Product, store_id: uuid.UUID | None) -> tuple[float, float]:
    """Returns (selling_price, mrp) for this product at this store, applying
    store/city override precedence. mrp is never overridden by a price list —
    only the selling price is, per StorePriceOverride's actual schema."""
    central_price = float(product.selling_price)
    mrp = float(product.mrp)
    if store_id is None:
        return central_price, mrp

    today = date.today()
    overrides = (
        await db.execute(
            select(StorePriceOverride).where(
                StorePriceOverride.product_id == product.id,
                StorePriceOverride.active.is_(True),
            )
        )
    ).scalars().all()
    live_overrides = [
        o
        for o in overrides
        if (o.start_date is None or o.start_date <= today) and (o.end_date is None or o.end_date >= today)
    ]
    if not live_overrides:
        return central_price, mrp

    store_match = next((o for o in live_overrides if o.store_id == store_id), None)
    if store_match is not None:
        return float(store_match.custom_selling_price), mrp

    store = await db.get(Store, store_id)
    city = store.city if store is not None else None
    if city:
        city_match = next((o for o in live_overrides if o.store_id is None and o.city == city), None)
        if city_match is not None:
            return float(city_match.custom_selling_price), mrp

    return central_price, mrp
