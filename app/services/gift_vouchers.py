"""Gift voucher issuance and redemption (Point 4 audit fix). PaymentModeMaster
listed "gift_voucher" as a tender code with nothing behind it — no issuance,
no balance, no redemption anywhere in the codebase."""

import secrets
import string
import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models_phase2 import GiftVoucher


def _generate_code() -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "GV-" + "".join(secrets.choice(alphabet) for _ in range(10))


async def issue_voucher(
    db: AsyncSession, *, initial_value: float, customer_id: uuid.UUID | None, issued_by: uuid.UUID, expires_at
) -> GiftVoucher:
    voucher = GiftVoucher(
        code=_generate_code(),
        initial_value=initial_value,
        balance=initial_value,
        issued_to_customer_id=customer_id,
        issued_by=issued_by,
        expires_at=expires_at,
    )
    db.add(voucher)
    await db.flush()
    return voucher


async def get_voucher_by_code(db: AsyncSession, *, code: str) -> GiftVoucher | None:
    return (await db.execute(select(GiftVoucher).where(GiftVoucher.code == code))).scalar_one_or_none()


async def redeem_voucher(db: AsyncSession, *, code: str, amount: float) -> GiftVoucher:
    voucher = await get_voucher_by_code(db, code=code)
    if voucher is None:
        raise HTTPException(status_code=404, detail=f"No gift voucher found for code {code}")
    if voucher.status != "active":
        raise HTTPException(status_code=409, detail=f"Gift voucher {code} is {voucher.status}, not active")
    if amount > float(voucher.balance):
        raise HTTPException(status_code=409, detail=f"Gift voucher {code} balance ₹{voucher.balance:.2f} is insufficient for ₹{amount:.2f}")
    voucher.balance = float(voucher.balance) - amount
    if voucher.balance <= 0:
        voucher.status = "redeemed"
    return voucher
