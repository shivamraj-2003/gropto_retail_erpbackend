"""Customer store-credit wallet (Point 4 audit fix). customer_wallet_ledgers
existed with zero write path anywhere — wallet was a payment mode in name
only. Same append-only-ledger pattern as loyalty.py, with the same
idempotent-replay safety via the (source_type, source_id, transaction_type)
unique constraint."""

import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models_phase4 import CustomerWalletLedger


class DuplicateWalletEntry(Exception):
    pass


class InsufficientWalletBalance(Exception):
    pass


async def get_wallet_balance(db: AsyncSession, *, customer_id: uuid.UUID) -> float:
    result = await db.execute(
        select(CustomerWalletLedger.amount, CustomerWalletLedger.transaction_type).where(
            CustomerWalletLedger.customer_id == customer_id
        )
    )
    total = 0.0
    for amount, transaction_type in result.all():
        total += float(amount) if transaction_type == "credit" else -float(amount)
    return total


async def credit_wallet(
    db: AsyncSession,
    *,
    customer_id: uuid.UUID,
    amount: float,
    reference_type: str,
    source_type: str,
    source_id: uuid.UUID,
) -> CustomerWalletLedger:
    balance = await get_wallet_balance(db, customer_id=customer_id)
    entry = CustomerWalletLedger(
        customer_id=customer_id,
        transaction_type="credit",
        amount=amount,
        reference_type=reference_type,
        source_type=source_type,
        source_id=source_id,
        balance_after=balance + amount,
    )
    db.add(entry)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise DuplicateWalletEntry() from exc
    return entry


async def debit_wallet(
    db: AsyncSession,
    *,
    customer_id: uuid.UUID,
    amount: float,
    reference_type: str,
    source_type: str,
    source_id: uuid.UUID,
) -> CustomerWalletLedger:
    """Unlike loyalty points (an internal, recoverable metric), a wallet
    represents real pre-paid value — overdraft is blocked, not flagged."""
    balance = await get_wallet_balance(db, customer_id=customer_id)
    if amount > balance:
        raise InsufficientWalletBalance(f"Wallet balance ₹{balance:.2f} is insufficient for ₹{amount:.2f}")
    entry = CustomerWalletLedger(
        customer_id=customer_id,
        transaction_type="debit",
        amount=amount,
        reference_type=reference_type,
        source_type=source_type,
        source_id=source_id,
        balance_after=balance - amount,
    )
    db.add(entry)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise DuplicateWalletEntry() from exc
    return entry
