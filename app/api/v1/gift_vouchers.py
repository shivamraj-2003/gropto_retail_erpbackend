import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission
from app.core.database import get_db
from app.models.models import Customer
from app.models.models_phase2 import GiftVoucher
from app.schemas.schemas_phase2 import GiftVoucherIssueIn, GiftVoucherOut
from app.services.audit import write_audit
from app.services.gift_vouchers import get_voucher_by_code, issue_voucher

router = APIRouter(prefix="/gift-vouchers", tags=["gift-vouchers"])


@router.post("", response_model=GiftVoucherOut, status_code=201)
async def issue_gift_voucher(
    payload: GiftVoucherIssueIn,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("pos.gift_voucher.create")),
) -> GiftVoucher:
    customer_id = None
    if payload.customer_phone:
        customer = (await db.execute(select(Customer).where(Customer.phone == payload.customer_phone))).scalar_one_or_none()
        if customer is None:
            customer = Customer(phone=payload.customer_phone)
            db.add(customer)
            await db.flush()
        customer_id = customer.id

    voucher = await issue_voucher(
        db, initial_value=payload.initial_value, customer_id=customer_id, issued_by=current.user_id, expires_at=payload.expires_at
    )
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=None,
        device_id=current.device_id,
        action="gift_voucher.issued",
        entity_type="gift_voucher",
        entity_id=voucher.id,
        new_value={"code": voucher.code, "initial_value": payload.initial_value},
    )
    await db.commit()
    await db.refresh(voucher)
    return voucher


@router.get("/{code}", response_model=GiftVoucherOut)
async def lookup_gift_voucher(
    code: str,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("pos.gift_voucher.view")),
) -> GiftVoucher:
    """Lets the POS validate a voucher (balance, status) before adding it as
    a tender, without having to attempt the sale first."""
    voucher = await get_voucher_by_code(db, code=code)
    if voucher is None:
        raise HTTPException(status_code=404, detail=f"No gift voucher found for code {code}")
    return voucher
