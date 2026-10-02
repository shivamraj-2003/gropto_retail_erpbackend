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
    # Point 10 audit fix: captured once the variance exceeds
    # cash.py::VARIANCE_TOLERANCE (not required below it).
    variance_reason: str | None = None


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
    discount_amount: float = 0
    tax_rate: float = 0


class PurchaseOrderCreate(BaseModel):
    vendor_id: uuid.UUID
    store_id: uuid.UUID | None = None
    requisition_id: uuid.UUID | None = None
    items: list[PoItemIn]


class PurchaseOrderCancelIn(BaseModel):
    reason: str | None = None


class GrnItemIn(BaseModel):
    product_id: uuid.UUID
    expected_qty: float
    received_qty: float
    batch_number: str | None = None
    mfg_date: date | None = None
    expiry_date: date | None = None
    qc_status: str = "accepted"
    # Point 6 audit fix: receiving used to accept any quantity for any
    # product against any PO with zero validation. Over-receipt beyond the
    # PO's ordered quantity now requires this explicit flag instead of
    # silently going through.
    allow_over_receipt: bool = False


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
    discount_amount: float
    tax_rate: float
    received_qty: float


class PurchaseOrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    vendor_id: uuid.UUID
    store_id: uuid.UUID | None
    status: str
    total_amount: float
    created_at: datetime
    items: list[PurchaseOrderItemOut]


# ---------------------------------------------------------------------------
# Vendor invoices / payables / payments (Point 6 audit fix)
# ---------------------------------------------------------------------------


class VendorInvoiceCreate(BaseModel):
    vendor_id: uuid.UUID
    purchase_order_id: uuid.UUID | None = None
    grn_id: uuid.UUID | None = None
    invoice_number: str
    invoice_date: date
    invoice_amount: float
    # Point 10 audit fix: purchase-side GST data didn't exist — all optional,
    # informational capture (not used by the existing amount-based 3-way match).
    taxable_value: float | None = None
    cgst_amount: float | None = None
    sgst_amount: float | None = None
    igst_amount: float | None = None
    place_of_supply: str | None = None


class VendorInvoiceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    vendor_id: uuid.UUID
    purchase_order_id: uuid.UUID | None
    grn_id: uuid.UUID | None
    invoice_number: str
    invoice_date: date
    invoice_amount: float
    taxable_value: float | None
    cgst_amount: float | None
    sgst_amount: float | None
    igst_amount: float | None
    place_of_supply: str | None
    status: str
    created_at: datetime


class ThreeWayMatchResultOut(BaseModel):
    invoice: VendorInvoiceOut
    po_amount: float | None
    grn_amount: float | None
    variance_amount: float
    match_status: str
    payable_id: uuid.UUID | None


class PayableOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    vendor_id: uuid.UUID
    vendor_invoice_id: uuid.UUID | None
    original_amount: float
    amount_due: float
    due_date: date | None
    status: str
    created_at: datetime


class VendorPaymentIn(BaseModel):
    amount: float
    reference: str | None = None
    # Point 10 audit fix: no idempotency protection existed on vendor
    # payments at all — a retried/double-clicked submission had no
    # server-side guard against double-paying. Optional for backward
    # compatibility; strongly recommended.
    idempotency_key: uuid.UUID | None = None


class VendorPaymentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    payable_id: uuid.UUID
    amount: float
    reference: str | None
    status: str
    created_at: datetime


class VendorPerformanceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    vendor_id: uuid.UUID
    snapshot_date: date
    fill_rate: float
    avg_lead_time_days: float
    rejection_rate: float
    price_variance_pct: float
    service_score: float


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
    is_active: bool


class WarehouseCreateIn(BaseModel):
    code: str
    name: str
    city: str | None = None


class WarehouseUpdateIn(BaseModel):
    name: str | None = None
    city: str | None = None
    is_active: bool | None = None


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
    # Point 10 audit fix: GST registration/place-of-supply data didn't exist
    # on Store at all.
    company_id: uuid.UUID | None = None
    gstin: str | None = None
    state: str | None = None
    is_active: bool = True


class StoreCreateIn(BaseModel):
    code: str
    name: str
    city: str | None = None
    cluster: str | None = None
    area_sqft: float | None = None
    target_revenue_monthly: float | None = None
    company_id: uuid.UUID | None = None
    gstin: str | None = None
    state: str | None = None


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
    company_id: uuid.UUID | None = None
    gstin: str | None = None
    state: str | None = None
    # Point 15 audit fix: the is_active column existed on Store from day
    # one, but no endpoint anywhere could ever set it to False — a closed
    # store could never be deactivated through the app.
    is_active: bool | None = None


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
    # Point 9 audit fix: points never expired under any code path. Null = no expiry.
    points_expiry_days: int | None = None


class LoyaltyConfigChange(BaseModel):
    earn_rate: float | None = None
    redeem_value: float | None = None
    min_balance_to_redeem: float | None = None
    max_redeem_share: float | None = None
    points_expiry_days: int | None = None
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
    assigned_to: uuid.UUID | None
    resolved_by: uuid.UUID | None
    resolved_at: datetime | None
    resolution_note: str | None
