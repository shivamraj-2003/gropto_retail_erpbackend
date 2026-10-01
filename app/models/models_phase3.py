"""Phase 3 — Enterprise & Omnichannel Expansion: OMS, CRM/customer intelligence,
HR/workforce, enterprise scalability support tables. Additive to Phase 1 + Phase 2."""

import uuid
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.models import uuid_pk

# ---------------------------------------------------------------------------
# Omnichannel OMS
# ---------------------------------------------------------------------------


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[uuid.UUID] = uuid_pk()
    channel: Mapped[str] = mapped_column(String, nullable=False)  # app, web, marketplace
    customer_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("customers.id"))
    allocated_store_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("stores.id"))
    status: Mapped[str] = mapped_column(
        String, default="placed"
    )  # placed, reserved, allocated, picking, picked, packed, dispatched, delivered, cancelled, returned, refunded
    subtotal: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    delivery_fee: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    # Point 9 audit fix: OMS had no promotion/coupon discount path at all —
    # unit_price was taken on faith from the client and nothing was ever
    # subtracted from subtotal for a promotion or coupon.
    discount_total: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    coupon_code: Mapped[str | None] = mapped_column(String)
    grand_total: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    delivery_address: Mapped[str | None] = mapped_column(Text)
    rider_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    delivery_otp: Mapped[str | None] = mapped_column(String)
    # Point 8 audit fix: OTP never expired and was returned to anyone who
    # could read the order, not just the assigned rider post-dispatch.
    delivery_otp_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Point 3 audit fix: blueprint's Picking/Packing Time KPIs had no stage
    # timestamps anywhere on Order. start_picking() stamps picking_started_at,
    # pick_order() (pack completion) stamps packed_at, dispatch_order() stamps
    # dispatched_at — giving three real, separately-measurable durations
    # instead of permanently-null placeholders.
    picking_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Point 8 audit fix: picking and packing used to be the same function
    # call — picked_by/packed_by are now distinct identities, picked_at vs
    # packed_at distinct timestamps, confirm_pack() a distinct status step.
    picked_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    picked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    packed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    packed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Point 8 audit fix: no payment field existed on Order at all — nothing
    # to refund, no way to even know whether an order was prepaid or COD.
    payment_mode: Mapped[str] = mapped_column(String, default="cod")  # cod, prepaid
    payment_reference: Mapped[str | None] = mapped_column(String)
    payment_status: Mapped[str] = mapped_column(
        String, default="pending"
    )  # pending, paid, cod_pending, refund_initiated, refunded
    # Point 8 audit fix: a retried "place order" call used to create a full
    # second order (and a second stock reservation) — no idempotency key existed.
    client_idempotency_key: Mapped[str | None] = mapped_column(String, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    items: Mapped[list["OrderItem"]] = relationship(cascade="all, delete-orphan", lazy="selectin")


class OrderStatusHistory(Base):
    """Point 8 audit fix: only the current status was ever stored — no
    history of how an order got there, when, or by whom."""

    __tablename__ = "order_status_history"
    id: Mapped[uuid.UUID] = uuid_pk()
    order_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("orders.id", ondelete="CASCADE"), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String)
    to_status: Mapped[str] = mapped_column(String, nullable=False)
    changed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OrderRefund(Base):
    """Point 8 audit fix: refund didn't exist anywhere in the OMS — nothing
    linked a cancellation/return back to the original payment."""

    __tablename__ = "order_refunds"
    id: Mapped[uuid.UUID] = uuid_pk()
    order_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("orders.id"), nullable=False)
    amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    method: Mapped[str] = mapped_column(String, nullable=False)  # razorpay, manual, not_required (cod never collected)
    gateway_refund_id: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="initiated")  # initiated, completed, failed
    reason: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OrderReturn(Base):
    """Point 8 audit fix: reverse logistics for online orders didn't exist —
    the in-store Return model is keyed to a POS Sale, not an Order."""

    __tablename__ = "order_returns"
    id: Mapped[uuid.UUID] = uuid_pk()
    order_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("orders.id"), nullable=False)
    requested_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    reason: Mapped[str | None] = mapped_column(Text)
    refund_total: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    status: Mapped[str] = mapped_column(String, default="pending")  # pending, completed
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    items: Mapped[list["OrderReturnItem"]] = relationship(cascade="all, delete-orphan", lazy="selectin")


class OrderReturnItem(Base):
    __tablename__ = "order_return_items"
    id: Mapped[uuid.UUID] = uuid_pk()
    return_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("order_returns.id", ondelete="CASCADE"))
    order_item_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("order_items.id"), nullable=False)
    product_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("products.id"), nullable=False)
    quantity: Mapped[float] = mapped_column(Numeric(12, 3), nullable=False)
    disposition: Mapped[str] = mapped_column(String, nullable=False)  # saleable, damaged
    refund_amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)


class StoreFootfall(Base):
    """Point 3 audit fix: blueprint's Conversion/Footfall KPI had no data
    source anywhere — no visitor-counting hardware integration exists, so
    this is a manual daily entry (store manager logs the day's walk-in
    count), same pattern as a cash-drawer count. One row per store per day;
    conversion = bills / footfall, computed from this against real sales."""

    __tablename__ = "store_footfall"
    id: Mapped[uuid.UUID] = uuid_pk()
    store_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("stores.id"), nullable=False)
    business_date: Mapped[date] = mapped_column(Date, nullable=False)
    footfall_count: Mapped[int] = mapped_column(Integer, nullable=False)
    recorded_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("store_id", "business_date", name="uq_store_footfall_store_date"),)


class OrderItem(Base):
    __tablename__ = "order_items"
    id: Mapped[uuid.UUID] = uuid_pk()
    order_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("orders.id", ondelete="CASCADE"))
    product_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("products.id"), nullable=False)
    quantity: Mapped[float] = mapped_column(Numeric(12, 3), nullable=False)
    unit_price: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    substituted_product_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("products.id"))
    picked_qty: Mapped[float | None] = mapped_column(Numeric(12, 3))


# ---------------------------------------------------------------------------
# CRM & customer intelligence
# ---------------------------------------------------------------------------


class CustomerEvent(Base):
    """Feeds RFM/cohort views: one row per meaningful customer touchpoint."""

    __tablename__ = "customer_events"
    id: Mapped[uuid.UUID] = uuid_pk()
    customer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("customers.id"), nullable=False)
    event_type: Mapped[str] = mapped_column(String, nullable=False)  # purchase, return, complaint, campaign_click
    source_type: Mapped[str | None] = mapped_column(String)
    source_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    value: Mapped[float | None] = mapped_column(Numeric(12, 2))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CustomerConsent(Base):
    __tablename__ = "customer_consent"
    customer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("customers.id"), primary_key=True)
    whatsapp_opt_in: Mapped[bool] = mapped_column(Boolean, default=False)
    sms_opt_in: Mapped[bool] = mapped_column(Boolean, default=False)
    email_opt_in: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Campaign(Base):
    __tablename__ = "campaigns"
    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String, nullable=False)
    channel: Mapped[str] = mapped_column(String, nullable=False)  # whatsapp, sms, push, email
    segment_query: Mapped[dict] = mapped_column(JSONB, nullable=False)  # audience builder criteria
    # WhatsApp marketing sends outside the 24h service window must use a
    # pre-approved Meta message template — created in Business Manager, only
    # referenced by name here.
    template_name: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="draft")  # draft, sending, sent, failed
    sent_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    # Point 11 audit fix: a campaign could only ever target a flat RFM-band
    # string — no way to point it at a real, reusable, multi-criteria
    # SavedAudience. created_by closes a prior audit-trail gap (no record of
    # who created a campaign at all).
    saved_audience_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("saved_audiences.id"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CampaignRecipient(Base):
    __tablename__ = "campaign_recipients"
    id: Mapped[uuid.UUID] = uuid_pk()
    campaign_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False)
    customer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("customers.id"), nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)  # sent, failed, skipped_no_consent
    provider_message_id: Mapped[str | None] = mapped_column(String)
    error: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # Point 11 audit fix: nothing stopped a campaign from being sent twice
    # and double-messaging every recipient — one row per (campaign,
    # customer) makes a resend idempotent (see services/crm.py's retry path,
    # which only targets customers with no prior "sent" row).
    __table_args__ = (UniqueConstraint("campaign_id", "customer_id", name="uq_campaign_recipient"),)


# ---------------------------------------------------------------------------
# HR & workforce
# ---------------------------------------------------------------------------


class Employee(Base):
    __tablename__ = "employees"
    id: Mapped[uuid.UUID] = uuid_pk()
    # Point 12 audit fix: Employee had no name field at all — the UI was
    # displaying `designation` (a job title, e.g. "Cashier") as the person's
    # identity label because there was nothing else to show.
    name: Mapped[str | None] = mapped_column(String)
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    store_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("stores.id"), nullable=False)
    # Point 12 audit fix: Department/Warehouse existed as standalone master
    # data with zero relationship to Employee — an employee could never
    # actually be assigned to either.
    department_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("departments.id"))
    warehouse_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("warehouses.id"))
    designation: Mapped[str] = mapped_column(String, nullable=False)
    reporting_manager_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("employees.id"))
    joined_at: Mapped[date | None] = mapped_column(Date)
    exited_at: Mapped[date | None] = mapped_column(Date)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Shift(Base):
    __tablename__ = "shifts"
    id: Mapped[uuid.UUID] = uuid_pk()
    store_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("stores.id"), nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    start_time: Mapped[str] = mapped_column(String, nullable=False)  # HH:MM
    end_time: Mapped[str] = mapped_column(String, nullable=False)


class Attendance(Base):
    __tablename__ = "attendance"
    id: Mapped[uuid.UUID] = uuid_pk()
    employee_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("employees.id"), nullable=False)
    shift_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("shifts.id"))
    attendance_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)  # present, absent, late, leave, weekly_off
    check_in: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    check_out: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (UniqueConstraint("employee_id", "attendance_date"),)


# ---------------------------------------------------------------------------
# Enterprise scalability & reliability
# ---------------------------------------------------------------------------


class DeviceConfig(Base):
    """Centralized configuration pushed to every device."""

    __tablename__ = "device_config"
    device_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("devices.id"), primary_key=True)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
