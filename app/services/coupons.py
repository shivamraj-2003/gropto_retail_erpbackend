"""Coupon codes (Point 9 audit fix). Previously "coupon" existed only as a
selectable string in a promo-type dropdown — no code, no validation, no
redemption tracking anywhere. Used by OMS order creation and by the till
(`check_coupon` for the live preview, `validate_and_apply_coupon` when the
bill is recorded)."""

import uuid
from datetime import date

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models_phase4 import Coupon, CouponRedemption


async def check_coupon(
    db: AsyncSession,
    *,
    code: str,
    customer_id: uuid.UUID | None,
    cart_subtotal: float,
) -> tuple[Coupon, float]:
    """Validates the coupon against date range/usage limits/min cart value and
    returns it with the capped discount. Writes nothing. Raises
    HTTPException(400) on any invalid/expired/exhausted coupon."""
    coupon = (await db.execute(select(Coupon).where(Coupon.code == code))).scalar_one_or_none()
    if coupon is None or not coupon.active:
        raise HTTPException(status_code=400, detail=f"Coupon {code} is invalid or inactive")

    today = date.today()
    if coupon.start_date and coupon.start_date > today:
        raise HTTPException(status_code=400, detail=f"Coupon {code} is not yet active")
    if coupon.end_date and coupon.end_date < today:
        raise HTTPException(status_code=400, detail=f"Coupon {code} has expired")
    if coupon.min_cart_value and cart_subtotal < float(coupon.min_cart_value):
        raise HTTPException(
            status_code=400,
            detail=f"Coupon {code} requires a minimum cart value of ₹{float(coupon.min_cart_value):.2f}",
        )

    if coupon.usage_limit_total is not None:
        total_used = (
            await db.execute(select(func.count()).select_from(CouponRedemption).where(CouponRedemption.coupon_id == coupon.id))
        ).scalar_one()
        if total_used >= coupon.usage_limit_total:
            raise HTTPException(status_code=400, detail=f"Coupon {code} has reached its usage limit")

    if coupon.usage_limit_per_customer is not None and customer_id is not None:
        customer_used = (
            await db.execute(
                select(func.count())
                .select_from(CouponRedemption)
                .where(CouponRedemption.coupon_id == coupon.id, CouponRedemption.customer_id == customer_id)
            )
        ).scalar_one()
        if customer_used >= coupon.usage_limit_per_customer:
            raise HTTPException(status_code=400, detail=f"You have already used coupon {code} the maximum number of times")

    if coupon.discount_type == "percent":
        discount = cart_subtotal * float(coupon.discount_value) / 100
    else:
        discount = float(coupon.discount_value)
    if coupon.max_discount_amount is not None:
        discount = min(discount, float(coupon.max_discount_amount))
    return coupon, round(max(min(discount, cart_subtotal), 0.0), 2)


async def validate_and_apply_coupon(
    db: AsyncSession,
    *,
    code: str,
    customer_id: uuid.UUID | None,
    cart_subtotal: float,
    source_type: str,
    source_id: uuid.UUID,
) -> float:
    """check_coupon plus the redemption row (idempotent under replay via the
    source_type/source_id/coupon_id unique constraint)."""
    coupon, discount = await check_coupon(db, code=code, customer_id=customer_id, cart_subtotal=cart_subtotal)
    if discount <= 0:
        return 0.0

    redemption = CouponRedemption(
        coupon_id=coupon.id,
        customer_id=customer_id,
        source_type=source_type,
        source_id=source_id,
        discount_amount=discount,
    )
    db.add(redemption)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=f"Coupon {code} was already applied to this order") from exc
    return discount
