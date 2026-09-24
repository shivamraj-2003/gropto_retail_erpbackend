"""Phase 2 — Operational ERP Expansion: warehouse, procurement expansion, store cash
operations, returns/refunds, advanced inventory, finance foundation, fraud/loss
prevention. Additive to the Phase 1 schema — no Phase 1 table is altered."""

import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
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
# Warehouse management
# ---------------------------------------------------------------------------


class Warehouse(Base):
    __tablename__ = "warehouses"
    id: Mapped[uuid.UUID] = uuid_pk()
    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    city: Mapped[str | None] = mapped_column(String)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class WarehouseLocation(Base):
    __tablename__ = "warehouse_locations"
    id: Mapped[uuid.UUID] = uuid_pk()
    warehouse_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("warehouses.id"), nullable=False)
    zone: Mapped[str] = mapped_column(String, nullable=False)
    rack: Mapped[str] = mapped_column(String, nullable=False)
    bin: Mapped[str] = mapped_column(String, nullable=False)

    __table_args__ = (UniqueConstraint("warehouse_id", "zone", "rack", "bin"),)


class Grn(Base):
    """Goods Receipt Note against a purchase order — variance vs expected captured on lines."""

    __tablename__ = "grn"
    id: Mapped[uuid.UUID] = uuid_pk()
    purchase_order_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("purchase_orders.id"))
    warehouse_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("warehouses.id"))
    store_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("stores.id"))
    received_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String, default="draft")  # draft, qc_hold, accepted, rejected
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    items: Mapped[list["GrnItem"]] = relationship(cascade="all, delete-orphan", lazy="selectin")


class GrnItem(Base):
    __tablename__ = "grn_items"
    id: Mapped[uuid.UUID] = uuid_pk()
    grn_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("grn.id", ondelete="CASCADE"))
    product_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("products.id"), nullable=False)
    expected_qty: Mapped[float] = mapped_column(Numeric(12, 3), default=0)
    received_qty: Mapped[float] = mapped_column(Numeric(12, 3), nullable=False)
    batch_number: Mapped[str | None] = mapped_column(String)
    expiry_date: Mapped[date | None] = mapped_column(Date)
    qc_status: Mapped[str] = mapped_column(String, default="accepted")  # accepted, rejected, damaged


class Transfer(Base):
    """Two-sided document: warehouse-to-store, store-to-store, store-to-warehouse."""

    __tablename__ = "transfers"
    id: Mapped[uuid.UUID] = uuid_pk()
    source_type: Mapped[str] = mapped_column(String, nullable=False)  # store, warehouse
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    dest_type: Mapped[str] = mapped_column(String, nullable=False)
    dest_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String, default="dispatched")  # dispatched, received, discrepancy
    dispatched_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    received_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    dispatched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    items: Mapped[list["TransferItem"]] = relationship(cascade="all, delete-orphan", lazy="selectin")


class TransferItem(Base):
    __tablename__ = "transfer_items"
    id: Mapped[uuid.UUID] = uuid_pk()
    transfer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("transfers.id", ondelete="CASCADE"))
    product_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("products.id"), nullable=False)
    dispatched_qty: Mapped[float] = mapped_column(Numeric(12, 3), nullable=False)
    received_qty: Mapped[float | None] = mapped_column(Numeric(12, 3))


# ---------------------------------------------------------------------------
# Procurement expansion
# ---------------------------------------------------------------------------


class PurchaseRequisition(Base):
    __tablename__ = "purchase_requisitions"
    id: Mapped[uuid.UUID] = uuid_pk()
    store_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("stores.id"), nullable=False)
    requested_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    status: Mapped[str] = mapped_column(String, default="draft")  # draft, submitted, converted, rejected
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    items: Mapped[list["PurchaseRequisitionItem"]] = relationship(cascade="all, delete-orphan", lazy="selectin")


class PurchaseRequisitionItem(Base):
    __tablename__ = "purchase_requisition_items"
    id: Mapped[uuid.UUID] = uuid_pk()
    requisition_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("purchase_requisitions.id", ondelete="CASCADE")
    )
    product_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("products.id"), nullable=False)
    quantity: Mapped[float] = mapped_column(Numeric(12, 3), nullable=False)


class VendorQuotation(Base):
    __tablename__ = "vendor_quotations"
    id: Mapped[uuid.UUID] = uuid_pk()
    vendor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("vendors.id"), nullable=False)
    product_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("products.id"), nullable=False)
    quoted_price: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    valid_until: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PurchaseOrder(Base):
    __tablename__ = "purchase_orders"
    id: Mapped[uuid.UUID] = uuid_pk()
    vendor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("vendors.id"), nullable=False)
    store_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("stores.id"))
    requisition_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("purchase_requisitions.id"))
    status: Mapped[str] = mapped_column(String, default="pending_approval")  # pending_approval, approved, received, closed
    total_amount: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    items: Mapped[list["PurchaseOrderItem"]] = relationship(cascade="all, delete-orphan", lazy="selectin")


class PurchaseOrderItem(Base):
    __tablename__ = "purchase_order_items"
    id: Mapped[uuid.UUID] = uuid_pk()
    purchase_order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("purchase_orders.id", ondelete="CASCADE")
    )
    product_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("products.id"), nullable=False)
    quantity: Mapped[float] = mapped_column(Numeric(12, 3), nullable=False)
    unit_cost: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)


class VendorPerformanceSnapshot(Base):
    """Refreshed nightly (scheduler job) rather than a live materialized view, for simplicity."""

    __tablename__ = "vendor_performance_snapshots"
    id: Mapped[uuid.UUID] = uuid_pk()
    vendor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("vendors.id"), nullable=False)
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    fill_rate: Mapped[float] = mapped_column(Numeric(5, 2), default=0)
    avg_lead_time_days: Mapped[float] = mapped_column(Numeric(6, 2), default=0)
    rejection_rate: Mapped[float] = mapped_column(Numeric(5, 2), default=0)

    __table_args__ = (UniqueConstraint("vendor_id", "snapshot_date"),)


# ---------------------------------------------------------------------------
# Store cash operations
# ---------------------------------------------------------------------------


class CashierShift(Base):
    __tablename__ = "cashier_shifts"
    id: Mapped[uuid.UUID] = uuid_pk()
    store_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("stores.id"), nullable=False)
    device_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("devices.id"), nullable=False)
    cashier_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    opening_float: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    expected_cash: Mapped[float | None] = mapped_column(Numeric(12, 2))
    counted_cash: Mapped[float | None] = mapped_column(Numeric(12, 2))
    variance: Mapped[float | None] = mapped_column(Numeric(12, 2))
    status: Mapped[str] = mapped_column(String, default="open")  # open, closed
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CashMovement(Base):
    __tablename__ = "cash_movements"
    id: Mapped[uuid.UUID] = uuid_pk()
    shift_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("cashier_shifts.id"), nullable=False)
    direction: Mapped[str] = mapped_column(String, nullable=False)  # in, out
    amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DayClose(Base):
    __tablename__ = "day_close"
    id: Mapped[uuid.UUID] = uuid_pk()
    store_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("stores.id"), nullable=False)
    business_date: Mapped[date] = mapped_column(Date, nullable=False)
    total_expected_cash: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    total_counted_cash: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    variance: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    closed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    closed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("store_id", "business_date"),)


# ---------------------------------------------------------------------------
# Returns & refunds
# ---------------------------------------------------------------------------


class Return(Base):
    __tablename__ = "returns"
    id: Mapped[uuid.UUID] = uuid_pk()
    sale_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("sales.id"), nullable=False)
    store_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("stores.id"), nullable=False)
    requested_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    refund_total: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    status: Mapped[str] = mapped_column(String, default="pending")  # pending, approved, rejected, completed
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    items: Mapped[list["ReturnItem"]] = relationship(cascade="all, delete-orphan", lazy="selectin")


class ReturnItem(Base):
    __tablename__ = "return_items"
    id: Mapped[uuid.UUID] = uuid_pk()
    return_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("returns.id", ondelete="CASCADE"))
    sale_item_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("sale_items.id"), nullable=False)
    product_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("products.id"), nullable=False)
    quantity: Mapped[float] = mapped_column(Numeric(12, 3), nullable=False)
    disposition: Mapped[str] = mapped_column(String, nullable=False)  # saleable, damaged, vendor_return
    refund_amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)


# ---------------------------------------------------------------------------
# Advanced inventory
# ---------------------------------------------------------------------------


class ReorderPoint(Base):
    __tablename__ = "reorder_points"
    product_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("products.id"), primary_key=True)
    store_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("stores.id"), primary_key=True)
    min_qty: Mapped[float] = mapped_column(Numeric(12, 3), default=0)
    max_qty: Mapped[float] = mapped_column(Numeric(12, 3), default=0)
    safety_stock: Mapped[float] = mapped_column(Numeric(12, 3), default=0)


# ---------------------------------------------------------------------------
# Finance foundation
# ---------------------------------------------------------------------------


class Expense(Base):
    __tablename__ = "expenses"
    id: Mapped[uuid.UUID] = uuid_pk()
    store_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("stores.id"), nullable=False)
    category: Mapped[str] = mapped_column(String, nullable=False)
    amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String, default="pending")  # pending, approved, rejected
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Payable(Base):
    __tablename__ = "payables"
    id: Mapped[uuid.UUID] = uuid_pk()
    vendor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("vendors.id"), nullable=False)
    purchase_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("purchases.id"))
    amount_due: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    due_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String, default="outstanding")  # outstanding, paid, overdue
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ---------------------------------------------------------------------------
# Fraud & loss prevention
# ---------------------------------------------------------------------------


class FraudAlert(Base):
    __tablename__ = "fraud_alerts"
    id: Mapped[uuid.UUID] = uuid_pk()
    store_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("stores.id"))
    rule_code: Mapped[str] = mapped_column(String, nullable=False)
    severity: Mapped[str] = mapped_column(String, default="medium")  # low, medium, high
    details: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String, default="open")  # open, reviewed, dismissed
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
