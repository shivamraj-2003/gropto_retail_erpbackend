"""The rules around a Paytm machine payment, kept free of database and network calls so they can be tested alone.

The router (api/v1/payments.py) loads rows, calls these, then saves. Paytm's own answers are read in paytm_pos.py.
"""

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from app.services.paytm_pos import (
    CANCEL_REQUESTED,
    CANCELLED,
    ENQUIRY_INTERVAL_SECONDS,
    FAILED,
    OPEN_STATES,
    SENT,
    SUCCESS,
    UNKNOWN,
    to_paise,
)

MAX_AMOUNT_RUPEES = Decimal("500000")
FINAL_STATES = (SUCCESS, FAILED, CANCELLED)
# Paytm has no record of a request that never reached it; after this long that answer can be trusted as "never sent"
NEVER_SENT_AFTER_SECONDS = 60
SLOW_AFTER_SECONDS = 300


class FlowError(Exception):
    def __init__(self, message: str, status_code: int = 409):
        super().__init__(message)
        self.status_code = status_code


def check_amount(amount: Decimal) -> Decimal:
    amount = Decimal(str(amount)).quantize(Decimal("0.01"))
    if amount <= 0:
        raise FlowError("The amount must be more than zero", 422)
    if amount > MAX_AMOUNT_RUPEES:
        raise FlowError(f"The amount is above the limit of ₹{MAX_AMOUNT_RUPEES:,.0f} for one machine payment", 422)
    return amount


def choose_terminal(terminals: list, requested_id: uuid.UUID | None):
    """The machine to use: the one asked for (it must be this store's and active), else the store's only one."""
    active = [t for t in terminals if t.is_active]
    if requested_id is not None:
        found = next((t for t in active if t.id == requested_id), None)
        if found is None:
            raise FlowError("That Paytm machine is not set up for this store")
        return found
    if not active:
        raise FlowError("No Paytm machine is set up for this store. Ask an admin to add one under Tills & Devices")
    if len(active) > 1:
        raise FlowError("This store has more than one Paytm machine. Choose which one to use")
    return active[0]


def blocking_payment(open_payments: list):
    """A request still waiting on the machine (or whose result is unknown) blocks a new one on the same machine."""
    for p in open_payments:
        if p.status in OPEN_STATES:
            return p
    return None


def blocking_message(p) -> str:
    if p.status == UNKNOWN:
        return "A payment on this machine has an unknown result. Check it with Paytm (or cancel it) before sending another"
    return "This machine already has a payment waiting. Finish or cancel it first"


def seconds_since(moment: datetime | None, now: datetime) -> float | None:
    return None if moment is None else (now - moment).total_seconds()


def due_for_enquiry(p, now: datetime) -> bool:
    """Paytm asks for the first enquiry 10 seconds after the sale request, then every 10 seconds."""
    if p.status in FINAL_STATES:
        return False
    age = seconds_since(p.sent_at, now)
    if p.status == SENT and age is not None and age < ENQUIRY_INTERVAL_SECONDS:
        return False
    since = seconds_since(p.last_enquiry_at, now)
    return since is None or since >= ENQUIRY_INTERVAL_SECONDS


def next_state(p, answer_state: str, message: str, *, code: str, amount_ok: bool, now: datetime) -> tuple[str, str]:
    """The state after an enquiry. Money is only SUCCESS when Paytm says paid AND the amount is the one asked for;
    a request Paytm has no record of is FAILED only once it has had time to arrive."""
    if p.status in FINAL_STATES:
        return p.status, p.status_message or ""
    if answer_state == SUCCESS and not amount_ok:
        return UNKNOWN, "Paytm reports a payment of a different amount. Do not accept it; check with Paytm"
    if answer_state == UNKNOWN and code == "0404":
        age = seconds_since(p.created_at, now)
        if age is not None and age >= NEVER_SENT_AFTER_SECONDS and p.sent_at is None:
            return FAILED, "The request never reached Paytm. You can send it again"
    if answer_state == FAILED and p.status == CANCEL_REQUESTED:
        return SENT, message  # the cancel failed, the request is still on the machine
    return answer_state, message


def settle_error(p, *, store_id: uuid.UUID, amount: float | Decimal, sale_id: uuid.UUID) -> str | None:
    """Why a bill may NOT be settled with this machine payment (None = fine). Used when the till's bill reaches the server."""
    if p.store_id != store_id:
        return "This Paytm payment belongs to another store"
    if p.status != SUCCESS:
        return f"This Paytm payment is not confirmed as paid (status: {p.status})"
    if to_paise(p.amount) != to_paise(amount):
        return f"This Paytm payment is for ₹{p.amount}, not ₹{amount}"
    if p.sale_id is not None and p.sale_id != sale_id:
        return "This Paytm payment has already settled another bill"
    return None


def is_slow(p, now: datetime) -> bool:
    age = seconds_since(p.sent_at, now)
    return p.status == SENT and age is not None and age > SLOW_AFTER_SECONDS


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


__all__ = [
    "FINAL_STATES", "FlowError", "check_amount", "choose_terminal", "blocking_payment", "blocking_message",
    "due_for_enquiry", "next_state", "settle_error", "is_slow", "now_utc",
]
