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


class TransferItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    product_id: uuid.UUID
    dispatched_qty: float
    received_qty: float | None


class TransferDetailOut(TransferOut):
    items: list[TransferItemOut]


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


class RequisitionItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    product_id: uuid.UUID
    quantity: float


class RequisitionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    store_id: uuid.UUID
    status: str
    created_at: datetime
    items: list[RequisitionItemOut]


class PurchaseOrderItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    product_id: uuid.UUID
    quantity: float
    unit_cost: float


class PurchaseOrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    vendor_id: uuid.UUID
    store_id: uuid.UUID | None
    status: str
    total_amount: float
    created_at: datetime
    items: list[PurchaseOrderItemOut]


class RoleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    code: str
    name: str
    max_discount_percent: float
    max_discount_value: float


class WarehouseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    code: str
    name: str
    city: str | None


class StoreOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    code: str
    name: str
    city: str | None
    cluster: str | None


class StoreCreateIn(BaseModel):
    code: str
    name: str
    city: str | None = None
    cluster: str | None = None


class StoreCreateResult(BaseModel):
    status: str  # "created" | "pending_approval"
    store_id: uuid.UUID | None = None
    request_id: uuid.UUID | None = None


class StoreUpdateIn(BaseModel):
    """Code is deliberately not editable here — it's baked into every bill
    number a store's tills have already generated (storeCode-deviceCode-seq),
    so changing it after the fact would make historical bill numbers
    inconsistent with new ones."""
    name: str | None = None
    city: str | None = None
    cluster: str | None = None


class StoreUpdateResult(BaseModel):
    status: str  # "updated" | "pending_approval"
    request_id: uuid.UUID | None = None


class GrnOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    purchase_order_id: uuid.UUID | None
    store_id: uuid.UUID | None
    status: str
    created_at: datetime


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


class LoyaltyConfigOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    earn_rate: float
    redeem_value: float
    min_balance_to_redeem: float
    max_redeem_share: float


class LoyaltyConfigChange(BaseModel):
    earn_rate: float | None = None
    redeem_value: float | None = None
    min_balance_to_redeem: float | None = None
    max_redeem_share: float | None = None
    reason: str | None = None


class DiscountRuleCreate(BaseModel):
    name: str
    scope: str  # product, category, bill
    target_id: uuid.UUID | None = None
    percent: float | None = None
    flat_amount: float | None = None


class DiscountRuleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    scope: str
    target_id: uuid.UUID | None
    percent: float | None
    flat_amount: float | None
    active: bool


class DiscountRuleChange(BaseModel):
    percent: float | None = None
    flat_amount: float | None = None
    active: bool | None = None
    reason: str | None = None


class FraudAlertOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    store_id: uuid.UUID | None
    rule_code: str
    severity: str
    details: dict
    status: str
    created_at: datetime
