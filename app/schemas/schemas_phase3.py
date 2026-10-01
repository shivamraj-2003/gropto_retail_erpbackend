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


class OrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    channel: str
    customer_id: uuid.UUID | None
    allocated_store_id: uuid.UUID | None
    status: str
    subtotal: float
    grand_total: float
    delivery_address: str | None
    rider_id: uuid.UUID | None
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
    delivery_otp: str | None


class OrderPickItem(BaseModel):
    order_item_id: uuid.UUID
    picked_qty: float
    substituted_product_id: uuid.UUID | None = None


class OrderPickRequest(BaseModel):
    items: list[OrderPickItem]


class OrderDispatchRequest(BaseModel):
    rider_id: uuid.UUID


class OrderDeliverRequest(BaseModel):
    otp: str


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
