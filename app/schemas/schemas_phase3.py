import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict


# ---------------------------------------------------------------------------
# Omnichannel OMS
# ---------------------------------------------------------------------------

class OrderItemIn(BaseModel):
    product_id: uuid.UUID
    quantity: float
    unit_price: float


class OrderCreate(BaseModel):
    channel: str
    customer_phone: str
    preferred_store_id: uuid.UUID | None = None
    delivery_address: str | None = None
    items: list[OrderItemIn]
    # Point 8 audit fix: a retried "place order" call used to create a full
    # second order (and a second stock reservation) — no idempotency key
    # existed. Optional for backward compatibility; strongly recommended.
    client_idempotency_key: str | None = None
    payment_mode: str = "cod"  # cod, prepaid
    payment_reference: str | None = None  # pre-verified gateway payment id, required when prepaid
    # Point 9 audit fix: coupon_code didn't exist anywhere in OMS. unit_price
    # on each item is accepted for client display continuity only — the
    # server re-resolves the real applicable price server-side and ignores
    # the client's value (see services/pricing.py::resolve_price).
    coupon_code: str | None = None


class OrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    channel: str
    customer_id: uuid.UUID | None
    allocated_store_id: uuid.UUID | None
    status: str
    subtotal: float
    discount_total: float
    coupon_code: str | None
    grand_total: float
    delivery_address: str | None
    rider_id: uuid.UUID | None
    payment_mode: str
    payment_status: str
    created_at: datetime


class OrderItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    product_id: uuid.UUID
    quantity: float
    unit_price: float
    substituted_product_id: uuid.UUID | None
    picked_qty: float | None


class OrderDetailOut(OrderOut):
    items: list[OrderItemOut]
    # Point 8 audit fix: delivery_otp used to be in this general response,
    # readable by anyone who could view order detail — now omitted here and
    # only ever surfaced via the rider-scoped /orders/{id}/delivery-otp
    # endpoint, which checks the caller is the assigned rider.


class OrderPickItem(BaseModel):
    order_item_id: uuid.UUID
    picked_qty: float
    substituted_product_id: uuid.UUID | None = None
    # Point 8 audit fix: picking previously had zero scan verification —
    # when supplied, this is checked against the (possibly substituted)
    # product's barcode before the pick is accepted.
    scanned_barcode: str | None = None


class OrderPickRequest(BaseModel):
    items: list[OrderPickItem]


class OrderDispatchRequest(BaseModel):
    rider_id: uuid.UUID


class OrderDeliverRequest(BaseModel):
    otp: str


class OrderDeliveryOtpOut(BaseModel):
    otp: str
    expires_at: datetime | None


class OrderCancelIn(BaseModel):
    reason: str | None = None


class OrderReturnItemIn(BaseModel):
    order_item_id: uuid.UUID
    product_id: uuid.UUID
    quantity: float
    disposition: str  # saleable, damaged
    refund_amount: float


class OrderReturnCreate(BaseModel):
    reason: str | None = None
    items: list[OrderReturnItemIn]


class OrderReturnOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    order_id: uuid.UUID
    reason: str | None
    refund_total: float
    status: str
    created_at: datetime


class OrderRefundOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    order_id: uuid.UUID
    amount: float
    method: str
    status: str
    created_at: datetime


class OrderStatusHistoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    from_status: str | None
    to_status: str
    changed_by: uuid.UUID | None
    reason: str | None
    created_at: datetime


# ---------------------------------------------------------------------------
# CRM
# ---------------------------------------------------------------------------

class ConsentUpdate(BaseModel):
    whatsapp_opt_in: bool | None = None
    sms_opt_in: bool | None = None
    email_opt_in: bool | None = None


class CampaignCreate(BaseModel):
    name: str
    channel: str
    segment_query: dict
    template_name: str | None = None


class CampaignOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    channel: str
    segment_query: dict
    template_name: str | None
    status: str
    sent_count: int
    failed_count: int
    created_at: datetime


class CampaignSendResult(BaseModel):
    status: str
    sent_count: int
    failed_count: int
    skipped_no_consent: int


# ---------------------------------------------------------------------------
# HR
# ---------------------------------------------------------------------------

class EmployeeCreate(BaseModel):
    user_id: uuid.UUID | None = None
    store_id: uuid.UUID
    designation: str
    reporting_manager_id: uuid.UUID | None = None
    joined_at: date | None = None


class EmployeeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    user_id: uuid.UUID | None
    store_id: uuid.UUID
    designation: str
    joined_at: date | None
    is_active: bool


class ShiftCreate(BaseModel):
    store_id: uuid.UUID
    name: str
    start_time: str
    end_time: str


class ShiftOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    store_id: uuid.UUID
    name: str
    start_time: str
    end_time: str


class AttendanceMark(BaseModel):
    employee_id: uuid.UUID
    shift_id: uuid.UUID | None = None
    attendance_date: date
    status: str
    check_in: datetime | None = None
    check_out: datetime | None = None


class AttendanceOut(BaseModel):
    date: date
    status: str
