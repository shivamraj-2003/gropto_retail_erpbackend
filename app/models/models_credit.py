import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.models import uuid_pk


class CreditEntry(Base):
    """One line of a customer's credit (udhaar) account at a store. The amount carries its own sign, so a customer's balance is
    simply the sum: positive = the customer owes the shop, negative = the shop owes the customer (paid too much)."""

    __tablename__ = "credit_entries"
    __table_args__ = (UniqueConstraint("sale_id", "kind", name="uq_credit_entry_sale_kind"),)
    id: Mapped[uuid.UUID] = uuid_pk()
    customer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("customers.id"), nullable=False, index=True)
    store_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("stores.id"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String, nullable=False)  # sale (a bill paid on credit), payment (money received), adjustment
    amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)  # + owed to the shop, - paid / forgiven
    mode: Mapped[str | None] = mapped_column(String)  # how a payment was received: cash, upi, card, bank
    sale_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("sales.id"))
    note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
