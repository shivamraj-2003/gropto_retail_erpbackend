import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.models import uuid_pk


class PaytmTerminal(Base):
    """One Paytm card machine and the store it belongs to. MID/TID are identifiers, not secrets
    (the merchant key lives only in the server's environment)."""

    __tablename__ = "paytm_terminals"
    __table_args__ = (UniqueConstraint("mid", "tid", name="uq_paytm_terminal_mid_tid"),)
    id: Mapped[uuid.UUID] = uuid_pk()
    store_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("stores.id"), nullable=False, index=True)
    mid: Mapped[str] = mapped_column(String, nullable=False)
    tid: Mapped[str] = mapped_column(String, nullable=False)
    device_serial: Mapped[str | None] = mapped_column(String)
    label: Mapped[str | None] = mapped_column(String)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PaytmPosPayment(Base):
    """One request sent to a Paytm machine. Kept forever: it is the evidence behind a card/UPI tender."""

    __tablename__ = "paytm_pos_payments"
    id: Mapped[uuid.UUID] = uuid_pk()
    store_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("stores.id"), nullable=False, index=True)
    terminal_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("paytm_terminals.id"), nullable=False, index=True)
    merchant_transaction_id: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="created", index=True)
    status_message: Mapped[str | None] = mapped_column(String)
    receipt_label: Mapped[str | None] = mapped_column(String)  # what the till called this bill while it was being paid
    result_code: Mapped[str | None] = mapped_column(String)
    paytm_details: Mapped[dict | None] = mapped_column(JSONB)  # bank references returned once paid (no card numbers)
    # The sale this payment settled. Unique, so one payment can never settle two bills.
    sale_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("sales.id"), unique=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_enquiry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    enquiry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
