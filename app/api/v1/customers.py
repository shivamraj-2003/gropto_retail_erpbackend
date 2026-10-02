from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission
from app.api.v1.loyalty import get_or_create_loyalty_config
from app.core.database import get_db
from app.models.models import Customer
from app.schemas.schemas_phase2 import CustomerLookupOut
from app.services.loyalty import get_balance as get_loyalty_balance
from app.services.wallet import get_wallet_balance

router = APIRouter(prefix="/customers", tags=["customers"])


@router.get("/lookup", response_model=CustomerLookupOut)
async def lookup_customer(
    phone: str,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("pos.customer.view")),
) -> dict:
    """Point 4 audit fix: the POS had no way to show a cashier how many
    loyalty points or how much wallet balance a customer could actually
    redeem — redemption/wallet-tender UI needs this to validate before
    the sale is even attempted."""
    customer = (await db.execute(select(Customer).where(Customer.phone == phone))).scalar_one_or_none()
    if customer is None:
        raise HTTPException(status_code=404, detail="No customer found for this phone number")

    config = await get_or_create_loyalty_config(db)
    loyalty_balance = await get_loyalty_balance(db, customer_id=customer.id)
    wallet_balance = await get_wallet_balance(db, customer_id=customer.id)

    return {
        "customer_id": customer.id,
        "phone": customer.phone,
        "loyalty_balance_points": loyalty_balance,
        "loyalty_redeemable_value": round(loyalty_balance * float(config.redeem_value), 2)
        if loyalty_balance >= float(config.min_balance_to_redeem)
        else 0.0,
        "wallet_balance": wallet_balance,
    }
