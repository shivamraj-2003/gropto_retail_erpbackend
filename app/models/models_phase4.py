import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.models import uuid_pk


# ---------------------------------------------------------------------------
# WMS & Batch Inventory (Section 5)
# ---------------------------------------------------------------------------

class InventoryBatch(Base):
    __tablename__ = "inventory_batches"
    id: Mapped[uuid.UUID] = uuid_pk()
    product_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("products.id"), nullable=False)
    store_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("stores.id"))
    warehouse_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("warehouses.id"))
    # Point 5 audit fix: batches had no location at all — zone/rack/bin was a
    # disconnected reference table nothing was ever actually placed in.
    # Populated by the putaway-confirm step (wms.py).
    location_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("warehouse_zone_locations.id"))
    batch_number: Mapped[str] = mapped_column(String, nullable=False)
    mfg_date: Mapped[date | None] = mapped_column(Date)
    expiry_date: Mapped[date | None] = mapped_column(Date)
    quantity: Mapped[float] = mapped_column(Numeric(12, 3), default=0)
    purchase_cost: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WarehouseZoneLocation(Base):
    __tablename__ = "warehouse_zone_locations"
    id: Mapped[uuid.UUID] = uuid_pk()
    warehouse_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("warehouses.id"), nullable=False)
    zone: Mapped[str] = mapped_column(String, nullable=False)  # e.g. Zone A, Frozen, Dry
    rack: Mapped[str] = mapped_column(String, nullable=False)  # e.g. R-01
    bin: Mapped[str] = mapped_column(String, nullable=False)   # e.g. B-04
    capacity: Mapped[float] = mapped_column(Numeric(12, 3), default=1000.0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class PutawayTask(Base):
    __tablename__ = "putaway_tasks"
    id: Mapped[uuid.UUID] = uuid_pk()
    grn_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("grn.id"), nullable=False)
    product_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("products.id"), nullable=False)
    suggested_location_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("warehouse_zone_locations.id"))
    confirmed_location_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("warehouse_zone_locations.id"))
    status: Mapped[str] = mapped_column(String, default="pending")  # pending, completed


class StoreIndent(Base):
    __tablename__ = "store_indents"
    id: Mapped[uuid.UUID] = uuid_pk()
    store_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("stores.id"), nullable=False)
    warehouse_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("warehouses.id"), nullable=False)
    requested_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    priority: Mapped[str] = mapped_column(String, default="normal")  # low, normal, high, urgent
    reason: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String, default="submitted")  # draft, submitted, converted_to_transfer, cancelled
    transfer_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("transfers.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    items: Mapped[list["StoreIndentItem"]] = relationship(cascade="all, delete-orphan", lazy="selectin")


class StoreIndentItem(Base):
    """Point 5 audit fix: StoreIndent previously had no way to express what or
    how much a store was actually requesting — a header row with no body."""

    __tablename__ = "store_indent_items"
    id: Mapped[uuid.UUID] = uuid_pk()
    indent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("store_indents.id", ondelete="CASCADE"))
    product_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("products.id"), nullable=False)
    quantity: Mapped[float] = mapped_column(Numeric(12, 3), nullable=False)


# ---------------------------------------------------------------------------
# Procurement & 3-Way Match (Section 6)
# ---------------------------------------------------------------------------

class VendorRfq(Base):
    __tablename__ = "vendor_rfqs"
    id: Mapped[uuid.UUID] = uuid_pk()
    requisition_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("purchase_requisitions.id"), nullable=False)
    vendor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("vendors.id"), nullable=False)
    quoted_unit_cost: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    lead_time_days: Mapped[int] = mapped_column(Integer, default=3)
    status: Mapped[str] = mapped_column(String, default="quoted")  # quoted, accepted, rejected


class VendorInvoiceMatch(Base):
    __tablename__ = "vendor_invoice_matches"
    id: Mapped[uuid.UUID] = uuid_pk()
    po_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("purchase_orders.id"), nullable=False)
    grn_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("grn.id"), nullable=False)
    vendor_invoice_no: Mapped[str] = mapped_column(String, nullable=False)
    po_amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    grn_amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    invoice_amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    variance_amount: Mapped[float] = mapped_column(Numeric(12, 2), default=0.0)
    status: Mapped[str] = mapped_column(String, default="matched")  # matched, discrepancy_flagged, approved


class VendorDebitCreditNote(Base):
    __tablename__ = "vendor_debit_credit_notes"
    id: Mapped[uuid.UUID] = uuid_pk()
    vendor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("vendors.id"), nullable=False)
    # Point 5 audit fix: nothing ever wrote a row here — a rejected/damaged
    # GRN line now auto-generates a debit note against the vendor.
    grn_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("grn.id"))
    grn_item_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("grn_items.id"))
    note_type: Mapped[str] = mapped_column(String, nullable=False)  # debit_note, credit_note
    amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String, default="issued")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ---------------------------------------------------------------------------
# Inventory Intelligence & Forecast (Section 7)
# ---------------------------------------------------------------------------

class AbcXyzMetric(Base):
    __tablename__ = "abc_xyz_metrics"
    id: Mapped[uuid.UUID] = uuid_pk()
    product_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("products.id"), nullable=False)
    store_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("stores.id"), nullable=False)
    abc_class: Mapped[str] = mapped_column(String, default="B")  # A, B, C
    xyz_class: Mapped[str] = mapped_column(String, default="Y")  # X, Y, Z
    stock_turns: Mapped[float] = mapped_column(Numeric(12, 2), default=0.0)
    days_of_inventory: Mapped[float] = mapped_column(Numeric(12, 2), default=0.0)
    calculated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ExpiryForecastSnapshot(Base):
    __tablename__ = "expiry_forecast_snapshots"
    id: Mapped[uuid.UUID] = uuid_pk()
    store_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("stores.id"), nullable=False)
    exposure_7d: Mapped[float] = mapped_column(Numeric(12, 2), default=0.0)
    exposure_15d: Mapped[float] = mapped_column(Numeric(12, 2), default=0.0)
    exposure_30d: Mapped[float] = mapped_column(Numeric(12, 2), default=0.0)
    exposure_60d: Mapped[float] = mapped_column(Numeric(12, 2), default=0.0)
    exposure_90d: Mapped[float] = mapped_column(Numeric(12, 2), default=0.0)
    total_value_at_risk: Mapped[float] = mapped_column(Numeric(12, 2), default=0.0)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ---------------------------------------------------------------------------
# Pricing, Promotions & Loyalty (Section 9)
# ---------------------------------------------------------------------------

class StorePriceOverride(Base):
    __tablename__ = "store_price_overrides"
    id: Mapped[uuid.UUID] = uuid_pk()
    product_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("products.id"), nullable=False)
    store_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("stores.id"))
    city: Mapped[str | None] = mapped_column(String)
    custom_selling_price: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class PromotionRule(Base):
    __tablename__ = "promotion_rules"
    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String, nullable=False)
    promo_type: Mapped[str] = mapped_column(String, nullable=False)  # bogo, combo, category_percent, coupon
    conditions_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    discount_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class ScheduledPriceChange(Base):
    __tablename__ = "scheduled_price_changes"
    id: Mapped[uuid.UUID] = uuid_pk()
    product_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("products.id"), nullable=False)
    new_sp: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    new_mrp: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String, default="pending_approval")  # pending_approval, approved, executed


class CustomerWalletLedger(Base):
    """Point 4 audit fix: this table existed but nothing ever wrote to it —
    wallet was listed as a payment mode in PaymentModeMaster with no actual
    debit/credit path. source_type/source_id + the unique constraint give it
    the same idempotent-replay safety loyalty_ledger already has."""

    __tablename__ = "customer_wallet_ledgers"
    id: Mapped[uuid.UUID] = uuid_pk()
    customer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("customers.id"), nullable=False)
    transaction_type: Mapped[str] = mapped_column(String, nullable=False)  # credit, debit
    amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    reference_type: Mapped[str] = mapped_column(String, default="store_credit")
    source_type: Mapped[str] = mapped_column(String, nullable=False, default="manual")
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, server_default=func.gen_random_uuid())
    balance_after: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("source_type", "source_id", "transaction_type"),)


# ---------------------------------------------------------------------------
# Finance, Bank Deposits & Budgets (Section 10)
# ---------------------------------------------------------------------------

class BankDeposit(Base):
    __tablename__ = "bank_deposits"
    id: Mapped[uuid.UUID] = uuid_pk()
    store_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("stores.id"), nullable=False)
    business_date: Mapped[date] = mapped_column(Date, nullable=False)
    cash_expected: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    cash_deposited: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    bank_name: Mapped[str] = mapped_column(String, nullable=False)
    slip_reference: Mapped[str] = mapped_column(String, nullable=False)
    variance: Mapped[float] = mapped_column(Numeric(12, 2), default=0.0)
    status: Mapped[str] = mapped_column(String, default="reconciled")


class StoreBudget(Base):
    __tablename__ = "store_budgets"
    id: Mapped[uuid.UUID] = uuid_pk()
    store_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("stores.id"), nullable=False)
    financial_year: Mapped[int] = mapped_column(Integer, nullable=False)
    month: Mapped[int] = mapped_column(Integer, nullable=False)
    capex_budget: Mapped[float] = mapped_column(Numeric(12, 2), default=0.0)
    opex_budget: Mapped[float] = mapped_column(Numeric(12, 2), default=0.0)
    actual_opex: Mapped[float] = mapped_column(Numeric(12, 2), default=0.0)


class CustomerReceivable(Base):
    __tablename__ = "customer_receivables"
    id: Mapped[uuid.UUID] = uuid_pk()
    customer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("customers.id"), nullable=False)
    sale_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("sales.id"))
    amount_due: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    due_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String, default="outstanding")


# ---------------------------------------------------------------------------
# CRM & Support (Section 11)
# ---------------------------------------------------------------------------

class CustomerServiceTicket(Base):
    __tablename__ = "customer_service_tickets"
    id: Mapped[uuid.UUID] = uuid_pk()
    customer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("customers.id"), nullable=False)
    sale_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("sales.id"))
    category: Mapped[str] = mapped_column(String, nullable=False)  # complaint, refund_request, query
    priority: Mapped[str] = mapped_column(String, default="medium")
    subject: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String, default="open")  # open, in_progress, resolved, closed
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RfmCohortSnapshot(Base):
    __tablename__ = "rfm_cohort_snapshots"
    id: Mapped[uuid.UUID] = uuid_pk()
    customer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("customers.id"), nullable=False)
    recency_score: Mapped[int] = mapped_column(Integer, default=5)
    frequency_score: Mapped[int] = mapped_column(Integer, default=1)
    monetary_score: Mapped[int] = mapped_column(Integer, default=1)
    segment: Mapped[str] = mapped_column(String, default="new_customer")  # champions, loyal, at_risk, hibernating
    churn_risk_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    calculated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ---------------------------------------------------------------------------
# HR & Workforce (Section 12)
# ---------------------------------------------------------------------------

class StaffTransfer(Base):
    __tablename__ = "staff_transfers"
    id: Mapped[uuid.UUID] = uuid_pk()
    employee_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("employees.id"), nullable=False)
    from_store_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("stores.id"), nullable=False)
    to_store_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("stores.id"), nullable=False)
    transfer_date: Mapped[date] = mapped_column(Date, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String, default="approved")


class PayrollSummaryExport(Base):
    __tablename__ = "payroll_summary_exports"
    id: Mapped[uuid.UUID] = uuid_pk()
    store_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("stores.id"), nullable=False)
    month_year: Mapped[str] = mapped_column(String, nullable=False)  # e.g. "2026-09"
    total_employees: Mapped[int] = mapped_column(Integer, default=0)
    worked_days: Mapped[float] = mapped_column(Numeric(12, 1), default=0.0)
    overtime_hours: Mapped[float] = mapped_column(Numeric(12, 1), default=0.0)
    penalties_total: Mapped[float] = mapped_column(Numeric(12, 2), default=0.0)
    incentives_total: Mapped[float] = mapped_column(Numeric(12, 2), default=0.0)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ---------------------------------------------------------------------------
# CEO Alerts & Fraud Control Tower (Section 13 & 17)
# ---------------------------------------------------------------------------

class CeoAlert(Base):
    __tablename__ = "ceo_alerts"
    id: Mapped[uuid.UUID] = uuid_pk()
    alert_type: Mapped[str] = mapped_column(String, nullable=False)  # sales_decline, margin_erosion, oos_risk, expiry_exposure, cash_mismatch, transfer_delay
    severity: Mapped[str] = mapped_column(String, default="high")  # medium, high, critical
    store_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("stores.id"))
    title: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    action_required: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, default="active")  # active, resolved
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SuspiciousBillingLog(Base):
    __tablename__ = "suspicious_billing_logs"
    id: Mapped[uuid.UUID] = uuid_pk()
    store_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("stores.id"), nullable=False)
    cashier_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    risk_score: Mapped[int] = mapped_column(Integer, default=80)
    pattern_type: Mapped[str] = mapped_column(String, nullable=False)  # repeated_voids, off_hours, high_discount, price_override
    details_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    reviewed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ---------------------------------------------------------------------------
# Section 15 Master Data Extensions & WMS Pick List
# ---------------------------------------------------------------------------

class Company(Base):
    __tablename__ = "companies"
    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String, nullable=False)
    legal_entity_name: Mapped[str] = mapped_column(String, nullable=False)
    gstin: Mapped[str | None] = mapped_column(String)
    state: Mapped[str] = mapped_column(String, nullable=False)
    city: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Cluster(Base):
    __tablename__ = "clusters"
    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String, nullable=False)  # e.g. North Zone, South Cluster
    region_code: Mapped[str] = mapped_column(String, nullable=False)
    regional_manager_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Department(Base):
    __tablename__ = "departments"
    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String, nullable=False)  # e.g. Staples, FMCG, Dairy, Produce
    code: Mapped[str] = mapped_column(String, nullable=False)


class PaymentModeMaster(Base):
    __tablename__ = "payment_modes_master"
    id: Mapped[uuid.UUID] = uuid_pk()
    code: Mapped[str] = mapped_column(String, nullable=False)  # cash, upi, card, wallet, gift_voucher
    name: Mapped[str] = mapped_column(String, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class ReasonCodeMaster(Base):
    __tablename__ = "reason_codes_master"
    id: Mapped[uuid.UUID] = uuid_pk()
    category: Mapped[str] = mapped_column(String, nullable=False)  # damage, expiry, wastage, shrinkage, price_override, stock_adjustment
    code: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(String, nullable=False)
    requires_approval: Mapped[bool] = mapped_column(Boolean, default=True)


class ChartOfAccount(Base):
    __tablename__ = "chart_of_accounts"
    id: Mapped[uuid.UUID] = uuid_pk()
    account_code: Mapped[str] = mapped_column(String, nullable=False)
    account_name: Mapped[str] = mapped_column(String, nullable=False)
    account_type: Mapped[str] = mapped_column(String, nullable=False)  # Asset, Liability, Revenue, Expense, Equity


class WmsPickListTask(Base):
    __tablename__ = "wms_pick_list_tasks"
    id: Mapped[uuid.UUID] = uuid_pk()
    transfer_order_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    warehouse_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("warehouses.id"), nullable=False)
    product_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("products.id"), nullable=False)
    zone_location_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("warehouse_zone_locations.id"))
    requested_qty: Mapped[float] = mapped_column(Numeric(12, 3), nullable=False)
    picked_qty: Mapped[float] = mapped_column(Numeric(12, 3), default=0.0)
    picker_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String, default="pending")  # pending, picking, packed, dispatched
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

