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


class CouponCreate(BaseModel):
    code: str
    discount_type: str  # percent, flat
    discount_value: float
    min_cart_value: float | None = None
    max_discount_amount: float | None = None
    start_date: date | None = None
    end_date: date | None = None
    usage_limit_total: int | None = None
    usage_limit_per_customer: int | None = None
    active: bool = True


class CouponOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    code: str
    discount_type: str
    discount_value: float
    min_cart_value: float | None
    max_discount_amount: float | None
    start_date: date | None
    end_date: date | None
    usage_limit_total: int | None
    usage_limit_per_customer: int | None
    active: bool
    created_at: datetime


class PromotionRedemptionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    promotion_rule_id: uuid.UUID
    source_type: str
    source_id: uuid.UUID
    store_id: uuid.UUID | None
    discount_amount: float
    created_at: datetime


class WalletCreditIn(BaseModel):
    amount: float
    reason: str


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
    actual_capex: float


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
    assigned_to: uuid.UUID | None = None
    resolution_notes: str | None = None
    resolved_at: datetime | None = None
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


# ---------------------------------------------------------------------------
# HR: leave, document checklist, incentive/penalty workflow
# ---------------------------------------------------------------------------

class LeaveTypeIn(BaseModel):
    name: str
    code: str
    paid: bool = True
    default_annual_days: float = 0.0


class LeaveTypeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    code: str
    paid: bool
    default_annual_days: float
    is_active: bool


class LeaveBalanceSet(BaseModel):
    employee_id: uuid.UUID
    leave_type_id: uuid.UUID
    year: int
    allocated_days: float


class LeaveBalanceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    employee_id: uuid.UUID
    leave_type_id: uuid.UUID
    year: int
    allocated_days: float


class LeaveBalanceSummary(BaseModel):
    leave_type_id: uuid.UUID
    leave_type_name: str
    year: int
    allocated_days: float
    used_days: float
    pending_days: float
    remaining_days: float


class LeaveRequestCreate(BaseModel):
    employee_id: uuid.UUID
    leave_type_id: uuid.UUID
    start_date: date
    end_date: date
    reason: str | None = None


class LeaveDecision(BaseModel):
    approve: bool
    note: str | None = None


class LeaveRequestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    employee_id: uuid.UUID
    leave_type_id: uuid.UUID
    start_date: date
    end_date: date
    days: float
    reason: str | None
    status: str
    decided_by: uuid.UUID | None
    decided_at: datetime | None
    decision_note: str | None
    created_at: datetime


class DocumentChecklistItemIn(BaseModel):
    name: str
    applies_to: str  # joining, exit
    required: bool = True


class DocumentChecklistItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    applies_to: str
    required: bool
    is_active: bool


class EmployeeDocumentUpdate(BaseModel):
    status: str
    reference: str | None = None
    notes: str | None = None


class EmployeeDocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    employee_id: uuid.UUID
    checklist_item_id: uuid.UUID
    status: str
    reference: str | None
    notes: str | None
    updated_at: datetime


class HrAdjustmentCreate(BaseModel):
    employee_id: uuid.UUID
    store_id: uuid.UUID
    adjustment_type: str  # incentive, penalty
    amount: float
    month_year: str
    reason: str


class HrAdjustmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    employee_id: uuid.UUID
    store_id: uuid.UUID
    adjustment_type: str
    amount: float
    month_year: str
    reason: str
    status: str
    created_at: datetime


class ProductivityRow(BaseModel):
    employee_id: uuid.UUID
    name: str | None
    designation: str
    role_code: str | None
    worked_days: int
    late_count: int
    absent_count: int
    sales_count: int
    sales_revenue: float
    orders_delivered: int


class CeoAlertOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    alert_type: str
    severity: str
    store_id: uuid.UUID | None
    source_id: uuid.UUID | None
    title: str
    description: str
    action_required: str
    status: str
    created_at: datetime
    assigned_to: uuid.UUID | None
    resolved_by: uuid.UUID | None
    resolved_at: datetime | None
    resolution_note: str | None


class SuspiciousBillingLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    store_id: uuid.UUID
    cashier_id: uuid.UUID | None
    risk_score: int
    pattern_type: str
    details_json: dict
    status: str
    assigned_to: uuid.UUID | None
    resolved_by: uuid.UUID | None
    resolved_at: datetime | None
    resolution_note: str | None
    created_at: datetime


class AlertAssign(BaseModel):
    assignee_id: uuid.UUID


class AlertResolve(BaseModel):
    status: str | None = None  # "reviewed" or "dismissed" (fraud_alerts/suspicious_billing_logs only)
    note: str | None = None


# ---------------------------------------------------------------------------
# Master Data (§15) — company/cluster/department/payment-mode/reason-code/COA
# ---------------------------------------------------------------------------

class CompanyIn(BaseModel):
    name: str
    legal_entity_name: str
    gstin: str | None = None
    pan: str | None = None
    state: str
    city: str
    einvoice_applicable: bool = False
    aato_threshold: float | None = None
    is_active: bool = True


class CompanyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    legal_entity_name: str
    gstin: str | None
    pan: str | None
    state: str
    city: str
    einvoice_applicable: bool
    aato_threshold: float | None
    is_active: bool
    created_at: datetime


class StoreBudgetCreate(BaseModel):
    store_id: uuid.UUID
    financial_year: int
    month: int
    capex_budget: float = 0.0
    opex_budget: float = 0.0


class EInvoiceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    sale_id: uuid.UUID
    company_id: uuid.UUID | None
    status: str
    irn: str | None
    ack_no: str | None
    ack_date: datetime | None
    error_response: str | None
    retry_count: int
    cancelled_reason: str | None
    created_at: datetime
    updated_at: datetime


class EInvoiceCancelIn(BaseModel):
    reason: str


class ClusterIn(BaseModel):
    name: str
    region_code: str
    regional_manager_id: uuid.UUID | None = None
    is_active: bool = True


class ClusterOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    region_code: str
    regional_manager_id: uuid.UUID | None
    is_active: bool
    created_at: datetime


class DepartmentIn(BaseModel):
    name: str
    code: str
    is_active: bool = True


class DepartmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    code: str
    is_active: bool


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
    is_active: bool = True


class ReasonCodeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    category: str
    code: str
    description: str
    requires_approval: bool
    is_active: bool


class ChartOfAccountIn(BaseModel):
    account_code: str
    account_name: str
    account_type: str
    is_active: bool = True


class ChartOfAccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    account_code: str
    account_name: str
    account_type: str
    is_active: bool


class RegionIn(BaseModel):
    name: str
    code: str
    is_active: bool = True


class RegionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    code: str
    is_active: bool
    created_at: datetime


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
    # Unpicked stock in the task's warehouse (pending/picking tasks only).
    available_qty: float | None = None


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

# ---------------------------------------------------------------------------
# CRM Enriched Schemas
# ---------------------------------------------------------------------------

class Customer360Full(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    total_orders: int
    total_spend: float
    aov: float
    first_purchase: str | None
    last_purchase: str | None
    loyalty_balance: float
    purchase_frequency: float
    preferred_store: dict | None  # {id, name}
    preferred_categories: list[str]
    new_or_repeat: str
    rfm_segment: str | None
    churn_risk: bool | None
    wallet_balance: float
    loyalty_history: list[dict]
    coupon_history: list[dict]
    refund_history: list[dict]
    ticket_history: list[dict]
    order_history: list[dict]

class AudienceCriteria(BaseModel):
    segment: str | None = None
    min_spend: float | None = None
    max_spend: float | None = None
    min_orders: int | None = None
    min_recency_days: int | None = None
    max_recency_days: int | None = None
    store_id: uuid.UUID | None = None
    churn_risk: bool | None = None
    loyalty_tier_min_points: float | None = None
    has_used_coupon: bool | None = None
    has_refund: bool | None = None

class SavedAudienceCreate(BaseModel):
    name: str
    criteria: AudienceCriteria

class AudiencePreviewIn(BaseModel):
    criteria: AudienceCriteria

class SavedAudienceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    criteria: dict
    estimated_size: int
    created_at: datetime

class ConsentHistoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    customer_id: uuid.UUID
    channel: str
    old_value: bool
    new_value: bool
    source: str
    created_at: datetime

class TicketUpdate(BaseModel):
    status: str | None = None
    assigned_to: uuid.UUID | None = None
    resolution_notes: str | None = None

class ClvSnapshotOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    customer_id: uuid.UUID
    historic_clv: float
    predicted_clv: float
    avg_order_value: float
    purchase_frequency: float
    customer_lifespan_months: int
    segment: str
    calculated_at: datetime

class CampaignAnalyticsOut(BaseModel):
    campaign_id: uuid.UUID
    name: str
    channel: str
    status: str
    sent_count: int
    failed_count: int
    skipped_count: int
    delivery_rate: float
    created_at: datetime


class ChecklistItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    category: str
    frequency: str
    is_active: bool


class ChecklistCompletionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    checklist_item_id: uuid.UUID
    store_id: uuid.UUID
    business_date: date
    status: str
    completed_by: uuid.UUID | None
    completed_at: datetime | None
    notes: str | None


class ChecklistCompleteIn(BaseModel):
    notes: str | None = None
