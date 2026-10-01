import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict


# ---------------------------------------------------------------------------
# WMS & Batch Inventory
# ---------------------------------------------------------------------------

class InventoryBatchOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    product_id: uuid.UUID
    store_id: uuid.UUID | None
    warehouse_id: uuid.UUID | None
    location_id: uuid.UUID | None
    batch_number: str
    mfg_date: date | None
    expiry_date: date | None
    quantity: float
    purchase_cost: float
    created_at: datetime


class WarehouseZoneLocationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    warehouse_id: uuid.UUID
    zone: str
    rack: str
    bin: str
    capacity: float
    is_active: bool


class PutawayTaskCreate(BaseModel):
    grn_id: uuid.UUID
    product_id: uuid.UUID
    suggested_location_id: uuid.UUID | None = None


class PutawayTaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    grn_id: uuid.UUID
    product_id: uuid.UUID
    suggested_location_id: uuid.UUID | None
    confirmed_location_id: uuid.UUID | None
    status: str


class PutawayTaskConfirm(BaseModel):
    confirmed_location_id: uuid.UUID


class StoreIndentItemIn(BaseModel):
    product_id: uuid.UUID
    quantity: float


class StoreIndentItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    product_id: uuid.UUID
    quantity: float


class StoreIndentCreate(BaseModel):
    store_id: uuid.UUID
    warehouse_id: uuid.UUID
    priority: str = "normal"
    reason: str | None = None
    items: list[StoreIndentItemIn]


class StoreIndentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    store_id: uuid.UUID
    warehouse_id: uuid.UUID
    priority: str
    reason: str | None
    status: str
    transfer_id: uuid.UUID | None
    created_at: datetime
    items: list[StoreIndentItemOut] = []


# ---------------------------------------------------------------------------
# Procurement & 3-Way Match
# ---------------------------------------------------------------------------

class VendorRfqCreate(BaseModel):
    requisition_id: uuid.UUID
    vendor_id: uuid.UUID
    quoted_unit_cost: float
    lead_time_days: int = 3


class VendorRfqOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    requisition_id: uuid.UUID
    vendor_id: uuid.UUID
    quoted_unit_cost: float
    lead_time_days: int
    status: str


class VendorInvoiceMatchOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    po_id: uuid.UUID
    grn_id: uuid.UUID
    vendor_invoice_no: str
    po_amount: float
    grn_amount: float
    invoice_amount: float
    variance_amount: float
    status: str


class VendorNoteCreate(BaseModel):
    vendor_id: uuid.UUID
    note_type: str  # debit_note, credit_note
    amount: float
    reason: str


class VendorNoteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    vendor_id: uuid.UUID
    note_type: str
    amount: float
    reason: str
    status: str


# ---------------------------------------------------------------------------
# Inventory Intelligence
# ---------------------------------------------------------------------------

class AbcXyzOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    product_id: uuid.UUID
    store_id: uuid.UUID
    abc_class: str
    xyz_class: str
    stock_turns: float
    days_of_inventory: float
    calculated_at: datetime


class ExpiryForecastOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    store_id: uuid.UUID
    exposure_7d: float
    exposure_15d: float
    exposure_30d: float
    exposure_60d: float
    exposure_90d: float
    total_value_at_risk: float
    generated_at: datetime


# ---------------------------------------------------------------------------
# Pricing, Promotions & Wallet
# ---------------------------------------------------------------------------

class StorePriceOverrideCreate(BaseModel):
    product_id: uuid.UUID
    store_id: uuid.UUID | None = None
    city: str | None = None
    custom_selling_price: float


class StorePriceOverrideOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    product_id: uuid.UUID
    store_id: uuid.UUID | None
    city: str | None
    custom_selling_price: float
    active: bool


class ScheduledPriceChangeCreate(BaseModel):
    product_id: uuid.UUID
    new_sp: float
    new_mrp: float
    effective_at: datetime


class ScheduledPriceChangeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    product_id: uuid.UUID
    new_sp: float
    new_mrp: float
    effective_at: datetime
    status: str


class WalletLedgerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    customer_id: uuid.UUID
    transaction_type: str
    amount: float
    reference_type: str
    balance_after: float
    created_at: datetime


class PromotionRuleIn(BaseModel):
    name: str
    promo_type: str  # bogo, combo, category_offer, coupon, cart_rule
    conditions_json: dict
    discount_json: dict
    active: bool = True


class PromotionRuleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    promo_type: str
    conditions_json: dict
    discount_json: dict
    active: bool


# ---------------------------------------------------------------------------
# Finance & Accounting
# ---------------------------------------------------------------------------

class BankDepositCreate(BaseModel):
    store_id: uuid.UUID
    business_date: date
    cash_expected: float
    cash_deposited: float
    bank_name: str
    slip_reference: str


class BankDepositOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    store_id: uuid.UUID
    business_date: date
    cash_expected: float
    cash_deposited: float
    bank_name: str
    slip_reference: str
    variance: float
    status: str


class StoreBudgetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    store_id: uuid.UUID
    financial_year: int
    month: int
    capex_budget: float
    opex_budget: float
    actual_opex: float


class ReceivableOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    customer_id: uuid.UUID
    sale_id: uuid.UUID | None
    amount_due: float
    due_date: date | None
    status: str


# ---------------------------------------------------------------------------
# CRM Support & Cohorts
# ---------------------------------------------------------------------------

class TicketCreate(BaseModel):
    customer_id: uuid.UUID
    sale_id: uuid.UUID | None = None
    category: str
    priority: str = "medium"
    subject: str
    description: str | None = None


class TicketOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    customer_id: uuid.UUID
    sale_id: uuid.UUID | None
    category: str
    priority: str
    subject: str
    description: str | None
    status: str
    created_at: datetime


class RfmCohortOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    customer_id: uuid.UUID
    recency_score: int
    frequency_score: int
    monetary_score: int
    segment: str
    churn_risk_flag: bool
    calculated_at: datetime


# ---------------------------------------------------------------------------
# HR & CEO Control Tower
# ---------------------------------------------------------------------------

class StaffTransferCreate(BaseModel):
    employee_id: uuid.UUID
    from_store_id: uuid.UUID
    to_store_id: uuid.UUID
    transfer_date: date
    reason: str | None = None


class StaffTransferOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    employee_id: uuid.UUID
    from_store_id: uuid.UUID
    to_store_id: uuid.UUID
    transfer_date: date
    reason: str | None
    status: str


class PayrollExportOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    store_id: uuid.UUID
    month_year: str
    total_employees: int
    worked_days: float
    overtime_hours: float
    penalties_total: float
    incentives_total: float
    generated_at: datetime


class CeoAlertOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    alert_type: str
    severity: str
    store_id: uuid.UUID | None
    title: str
    description: str
    action_required: str
    status: str
    created_at: datetime


# ---------------------------------------------------------------------------
# Master Data (§15) — company/cluster/department/payment-mode/reason-code/COA
# ---------------------------------------------------------------------------

class CompanyIn(BaseModel):
    name: str
    legal_entity_name: str
    gstin: str | None = None
    state: str
    city: str


class CompanyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    legal_entity_name: str
    gstin: str | None
    state: str
    city: str
    created_at: datetime


class ClusterIn(BaseModel):
    name: str
    region_code: str
    regional_manager_id: uuid.UUID | None = None


class ClusterOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    region_code: str
    regional_manager_id: uuid.UUID | None
    created_at: datetime


class DepartmentIn(BaseModel):
    name: str
    code: str


class DepartmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    code: str


class PaymentModeIn(BaseModel):
    code: str
    name: str
    is_active: bool = True


class PaymentModeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    code: str
    name: str
    is_active: bool


class ReasonCodeIn(BaseModel):
    category: str
    code: str
    description: str
    requires_approval: bool = True


class ReasonCodeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    category: str
    code: str
    description: str
    requires_approval: bool


class ChartOfAccountIn(BaseModel):
    account_code: str
    account_name: str
    account_type: str


class ChartOfAccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    account_code: str
    account_name: str
    account_type: str


# ---------------------------------------------------------------------------
# WMS pick / pack / dispatch (§5, §16 Warehouse-to-Store)
# ---------------------------------------------------------------------------

class PickListLineIn(BaseModel):
    warehouse_id: uuid.UUID
    product_id: uuid.UUID
    requested_qty: float
    transfer_order_id: uuid.UUID | None = None


class PickListCreate(BaseModel):
    lines: list[PickListLineIn]


class WmsPickListTaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    transfer_order_id: uuid.UUID | None
    warehouse_id: uuid.UUID
    product_id: uuid.UUID
    zone_location_id: uuid.UUID | None
    requested_qty: float
    picked_qty: float
    picker_id: uuid.UUID | None
    status: str
    created_at: datetime
    fefo_batch_number: str | None = None
    fefo_expiry_date: date | None = None


class PickConfirm(BaseModel):
    picked_qty: float
    # Point 5 audit fix: nothing previously validated that the picker actually
    # scanned the right item/location — these are optional for backwards
    # compatibility with any existing caller, but when supplied the backend
    # rejects a mismatch rather than trusting the quantity alone.
    scanned_barcode: str | None = None
    scanned_location_id: uuid.UUID | None = None


class PickListDispatch(BaseModel):
    pick_list_ids: list[uuid.UUID]
    dest_type: str
    dest_id: uuid.UUID
