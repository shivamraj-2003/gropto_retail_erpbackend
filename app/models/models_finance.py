import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.models import uuid_pk


class FinanceVoucher(Base):
    """A manual accounting entry (cash or bank receipt/payment, contra, journal, opening balance). It is posted into the ledger next
    to everything the system records by itself, so the ledger can carry what has no bill: money paid from the pocket, a bank charge,
    the balances brought forward."""

    __tablename__ = "finance_vouchers"
    __table_args__ = (UniqueConstraint("store_id", "voucher_no", name="uq_finance_voucher_no"),)
    id: Mapped[uuid.UUID] = uuid_pk()
    store_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("stores.id"), nullable=False, index=True)
    voucher_type: Mapped[str] = mapped_column(String, nullable=False)  # CASH RECEIPT, CASH PAYMENT, BANK RECEIPT, BANK PAYMENT, CONTRA, JOURNAL, OPENING
    voucher_no: Mapped[str] = mapped_column(String, nullable=False)
    voucher_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    main_account: Mapped[str | None] = mapped_column(String)  # the cash / bank account the money moved through (not for journals)
    main_account_name: Mapped[str | None] = mapped_column(String)
    narration: Mapped[str | None] = mapped_column(Text)
    total_amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    lines: Mapped[list["FinanceVoucherLine"]] = relationship(cascade="all, delete-orphan", lazy="selectin")


class FinanceVoucherLine(Base):
    __tablename__ = "finance_voucher_lines"
    id: Mapped[uuid.UUID] = uuid_pk()
    voucher_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("finance_vouchers.id", ondelete="CASCADE"), nullable=False, index=True)
    dc: Mapped[str] = mapped_column(String, nullable=False)  # D or C
    account_key: Mapped[str] = mapped_column(String, nullable=False)  # a ledger account key (cash, vendor:<id>, expense:<name>, custom:<name> ...)
    account_name: Mapped[str] = mapped_column(String, nullable=False)
    amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    mode_type: Mapped[str | None] = mapped_column(String)  # cheque, rtgs, neft, upi, cash, card
    mode_no: Mapped[str | None] = mapped_column(String)
    mode_date: Mapped[date | None] = mapped_column(Date)
