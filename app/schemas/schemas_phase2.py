import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict


# ---------------------------------------------------------------------------
# Returns & refunds
# ---------------------------------------------------------------------------

class ReturnItemIn(BaseModel):
    sale_item_id: uuid.UUID
    product_id: uuid.UUID
    quantity: float
    disposition: str  # saleable, damaged, vendor_return
    refund_amount: float


class ReturnCreate(BaseModel):
    sale_id: uuid.UUID
    store_id: uuid.UUID
    reason: str | None = None
    items: list[ReturnItemIn]


class ReturnOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    sale_id: uuid.UUID
    store_id: uuid.UUID
    refund_total: float
    status: str
    created_at: datetime


# ---------------------------------------------------------------------------
# Transfers
# ---------------------------------------------------------------------------

class TransferItemIn(BaseModel):
    product_id: uuid.UUID
    dispatched_qty: float


class TransferCreate(BaseModel):
    source_type: str
    source_id: uuid.UUID
    dest_type: str
    dest_id: uuid.UUID
    items: list[TransferItemIn]


class TransferReceiveItem(BaseModel):
    transfer_item_id: uuid.UUID
    received_qty: float


class TransferReceive(BaseModel):
    items: list[TransferReceiveItem]


class TransferOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    source_type: str
    source_id: uuid.UUID
    dest_type: str
    dest_id: uuid.UUID
    status: str


# ---------------------------------------------------------------------------
# Cash operations
# ---------------------------------------------------------------------------

class ShiftOpen(BaseModel):
    store_id: uuid.UUID
    device_id: uuid.UUID
    opening_float: float


class CashMovementIn(BaseModel):
    direction: str  # in, out
    amount: float
    reason: str


class ShiftClose(BaseModel):
    counted_cash: float


class DayCloseRequest(BaseModel):
    store_id: uuid.UUID
    business_date: date


# ---------------------------------------------------------------------------
# Procurement expansion
# ---------------------------------------------------------------------------

class RequisitionItemIn(BaseModel):
    product_id: uuid.UUID
    quantity: float


class RequisitionCreate(BaseModel):
    store_id: uuid.UUID
    items: list[RequisitionItemIn]


class PoItemIn(BaseModel):
    product_id: uuid.UUID
    quantity: float
    unit_cost: float


class PurchaseOrderCreate(BaseModel):
    vendor_id: uuid.UUID
    store_id: uuid.UUID | None = None
    requisition_id: uuid.UUID | None = None
    items: list[PoItemIn]


class GrnItemIn(BaseModel):
    product_id: uuid.UUID
    expected_qty: float
    received_qty: float
    batch_number: str | None = None
    expiry_date: date | None = None
    qc_status: str = "accepted"


class GrnCreate(BaseModel):
    purchase_order_id: uuid.UUID | None = None
    store_id: uuid.UUID | None = None
    items: list[GrnItemIn]


# ---------------------------------------------------------------------------
# Reorder points / finance / fraud
# ---------------------------------------------------------------------------

class ReorderPointSet(BaseModel):
    product_id: uuid.UUID
    store_id: uuid.UUID
    min_qty: float
    max_qty: float
    safety_stock: float = 0


class ExpenseCreate(BaseModel):
    store_id: uuid.UUID
    category: str
    amount: float
    description: str | None = None


class FraudAlertOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    store_id: uuid.UUID | None
    rule_code: str
    severity: str
    details: dict
    status: str
    created_at: datetime
