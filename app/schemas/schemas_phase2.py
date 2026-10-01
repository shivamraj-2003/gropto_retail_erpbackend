import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field


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
    is_exchange: bool = False
    items: list[ReturnItemIn]


class ReturnOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    sale_id: uuid.UUID
    store_id: uuid.UUID
    refund_total: float
    status: str
    is_exchange: bool
    exchange_sale_id: uuid.UUID | None
    created_at: datetime


class ReturnLinkExchangeIn(BaseModel):
    exchange_sale_id: uuid.UUID


# ---------------------------------------------------------------------------
# Stock count / cycle count
# ---------------------------------------------------------------------------


class StockCountCreate(BaseModel):
    store_id: uuid.UUID
    product_ids: list[uuid.UUID] | None = None  # None = every active product with a balance row at this store


class StockCountLineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    product_id: uuid.UUID
    expected_qty: float
    counted_qty: float | None
    variance: float | None


class StockCountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    store_id: uuid.UUID
    status: str
    initiated_by: uuid.UUID
    finalized_by: uuid.UUID | None
    created_at: datetime
    completed_at: datetime | None
    lines: list[StockCountLineOut] = []


class StockCountLineSubmit(BaseModel):
    product_id: uuid.UUID
    counted_qty: float


class StockCountSubmitIn(BaseModel):
    lines: list[StockCountLineSubmit]


# ---------------------------------------------------------------------------
# Gift vouchers
# ---------------------------------------------------------------------------


class GiftVoucherIssueIn(BaseModel):
    initial_value: float = Field(gt=0)
    customer_phone: str | None = None
    expires_at: date | None = None


class GiftVoucherOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    code: str
    initial_value: float
    balance: float
    status: str
    expires_at: date | None
    created_at: datetime


# ---------------------------------------------------------------------------
# Customer lookup (wallet/loyalty balance at the till)
# ---------------------------------------------------------------------------


class CustomerLookupOut(BaseModel):
    customer_id: uuid.UUID
    phone: str
    loyalty_balance_points: float
    loyalty_redeemable_value: float
    wallet_balance: float


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
    # Point 4 audit fix: optional notes/coins breakdown — when supplied, its
    # sum must match counted_cash (validated in cash.py service), giving a
    # real denomination count instead of only a lump total.
    denomination_breakdown: dict[str, int] | None = None


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
    mfg_date: date | None = None
    expiry_date: date | None = None
    qc_status: str = "accepted"


class GrnCreate(BaseModel):
    purchase_order_id: uuid.UUID | None = None
    store_id: uuid.UUID | None = None
    warehouse_id: uuid.UUID | None = None
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
    # Point 3 audit fix: back the blueprint's Sales/Sq Ft and Target
    # Achievement KPIs — null until someone sets a real value for the store.
    area_sqft: float | None = None
    target_revenue_monthly: float | None = None


class StoreCreateIn(BaseModel):
    code: str
    name: str
    city: str | None = None
    cluster: str | None = None
    area_sqft: float | None = None
    target_revenue_monthly: float | None = None


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
    area_sqft: float | None = None
    target_revenue_monthly: float | None = None


class StoreUpdateResult(BaseModel):
    status: str  # "updated" | "pending_approval"
    request_id: uuid.UUID | None = None


class StoreFootfallIn(BaseModel):
    business_date: date
    footfall_count: int = Field(ge=0)


class StoreFootfallOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    store_id: uuid.UUID
    business_date: date
    footfall_count: int


class GrnOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    grn_number: str | None
    purchase_order_id: uuid.UUID | None
    store_id: uuid.UUID | None
    status: str
    created_at: datetime


class TransferDiscrepancyResolveIn(BaseModel):
    note: str | None = None


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


class LoyaltyTierIn(BaseModel):
    name: str
    min_lifetime_points: float
    earn_rate_multiplier: float = 1.0


class LoyaltyTierOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    min_lifetime_points: float
    earn_rate_multiplier: float


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
