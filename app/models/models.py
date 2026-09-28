import uuid
from datetime import date, datetime

from sqlalchemy import (
    ARRAY,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    Numeric,
    Sequence,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

# Shared across products/categories/discount_rules so a device can pull every
# master-data table against a single watermark during sync.
global_revision_seq = Sequence("global_revision_seq")


def uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())


def revision_column() -> Mapped[int]:
    return mapped_column(
        server_default=text(f"nextval('{global_revision_seq.name}')"), nullable=False
    )


class Store(Base):
    __tablename__ = "stores"
    id: Mapped[uuid.UUID] = uuid_pk()
    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    city: Mapped[str | None] = mapped_column(String)
    cluster: Mapped[str | None] = mapped_column(String)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Role(Base):
    __tablename__ = "roles"
    id: Mapped[uuid.UUID] = uuid_pk()
    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    max_discount_percent: Mapped[float] = mapped_column(Numeric(5, 2), default=0)
    max_discount_value: Mapped[float] = mapped_column(Numeric(12, 2), default=0)


class Permission(Base):
    __tablename__ = "permissions"
    id: Mapped[uuid.UUID] = uuid_pk()
    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)


class RolePermission(Base):
    __tablename__ = "role_permissions"
    role_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("roles.id"), primary_key=True)
    permission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("permissions.id"), primary_key=True
    )


class User(Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = uuid_pk()
    email: Mapped[str | None] = mapped_column(String, unique=True)
    phone: Mapped[str | None] = mapped_column(String, unique=True)
    full_name: Mapped[str] = mapped_column(String, nullable=False)
    password_hash: Mapped[str] = mapped_column(String, nullable=False)
    role_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("roles.id"), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    role: Mapped["Role"] = relationship(lazy="joined")


class UserStore(Base):
    __tablename__ = "user_stores"
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), primary_key=True)
    store_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("stores.id"), primary_key=True)


class Device(Base):
    __tablename__ = "devices"
    id: Mapped[uuid.UUID] = uuid_pk()
    store_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("stores.id"), nullable=False)
    code: Mapped[str] = mapped_column(String, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String, default="pending")
    activation_code: Mapped[str | None] = mapped_column(String)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("store_id", "code"),)


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"
    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    device_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("devices.id"))
    token_hash: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    family_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Category(Base):
    __tablename__ = "categories"
    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String, nullable=False)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("categories.id"))
    revision: Mapped[int] = revision_column()


class Product(Base):
    __tablename__ = "products"
    id: Mapped[uuid.UUID] = uuid_pk()
    sku: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    barcode: Mapped[str | None] = mapped_column(String, unique=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    category_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("categories.id"))
    uom: Mapped[str] = mapped_column(String, default="EA")
    pack_size: Mapped[float] = mapped_column(Numeric(10, 3), default=1)
    purchase_price: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    selling_price: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    mrp: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    tax_rate: Mapped[float] = mapped_column(Numeric(5, 2), default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    revision: Mapped[int] = revision_column()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class InventoryBalance(Base):
    __tablename__ = "inventory_balances"
    product_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("products.id"), primary_key=True)
    store_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("stores.id"), primary_key=True)
    quantity: Mapped[float] = mapped_column(Numeric(14, 3), default=0)
    reserved: Mapped[float] = mapped_column(Numeric(14, 3), default=0)
    in_transit: Mapped[float] = mapped_column(Numeric(14, 3), default=0)
    damaged: Mapped[float] = mapped_column(Numeric(14, 3), default=0)
    blocked: Mapped[float] = mapped_column(Numeric(14, 3), default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class InventoryMovement(Base):
    __tablename__ = "inventory_movements"
    id: Mapped[uuid.UUID] = uuid_pk()
    product_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("products.id"), nullable=False)
    store_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("stores.id"), nullable=False)
    delta: Mapped[float] = mapped_column(Numeric(14, 3), nullable=False)
    reason_code: Mapped[str] = mapped_column(String, nullable=False)
    source_type: Mapped[str] = mapped_column(String, nullable=False)
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    device_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("devices.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("source_type", "source_id", "product_id"),)


class Customer(Base):
    __tablename__ = "customers"
    id: Mapped[uuid.UUID] = uuid_pk()
    phone: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LoyaltyLedger(Base):
    __tablename__ = "loyalty_ledger"
    id: Mapped[uuid.UUID] = uuid_pk()
    customer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("customers.id"), nullable=False)
    delta_points: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    reason: Mapped[str] = mapped_column(String, nullable=False)
    source_type: Mapped[str] = mapped_column(String, nullable=False)
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("source_type", "source_id", "reason"),)


class LoyaltyConfig(Base):
    __tablename__ = "loyalty_config"
    id: Mapped[bool] = mapped_column(Boolean, primary_key=True, default=True)
    earn_rate: Mapped[float] = mapped_column(Numeric(6, 4), default=0.01)
    redeem_value: Mapped[float] = mapped_column(Numeric(6, 4), default=0.5)
    min_balance_to_redeem: Mapped[float] = mapped_column(Numeric(12, 2), default=50)
    max_redeem_share: Mapped[float] = mapped_column(Numeric(4, 3), default=0.5)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DiscountRule(Base):
    __tablename__ = "discount_rules"
    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String, nullable=False)
    scope: Mapped[str] = mapped_column(String, nullable=False)
    target_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    percent: Mapped[float | None] = mapped_column(Numeric(5, 2))
    flat_amount: Mapped[float | None] = mapped_column(Numeric(12, 2))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    revision: Mapped[int] = revision_column()


class Sale(Base):
    __tablename__ = "sales"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    store_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("stores.id"), nullable=False)
    device_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("devices.id"), nullable=False)
    bill_number: Mapped[str] = mapped_column(String, nullable=False)
    cashier_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("customers.id"))
    subtotal: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    discount_total: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    tax_total: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    grand_total: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    loyalty_points_earned: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    loyalty_points_redeemed: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    override_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    override_reason: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String, default="completed")
    client_idempotency_key: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    billed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    items: Mapped[list["SaleItem"]] = relationship(cascade="all, delete-orphan", lazy="selectin")
    payments: Mapped[list["Payment"]] = relationship(cascade="all, delete-orphan", lazy="selectin")

    __table_args__ = (UniqueConstraint("store_id", "device_id", "bill_number"),)


class SaleItem(Base):
    __tablename__ = "sale_items"
    id: Mapped[uuid.UUID] = uuid_pk()
    sale_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("sales.id", ondelete="CASCADE"))
    product_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("products.id"), nullable=False)
    product_name_snapshot: Mapped[str] = mapped_column(String, nullable=False)
    tax_rate_snapshot: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False)
    quantity: Mapped[float] = mapped_column(Numeric(12, 3), nullable=False)
    unit_price: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    line_discount: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    line_total: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)


class Payment(Base):
    __tablename__ = "payments"
    id: Mapped[uuid.UUID] = uuid_pk()
    sale_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("sales.id", ondelete="CASCADE"))
    mode: Mapped[str] = mapped_column(String, nullable=False)
    amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    reference: Mapped[str | None] = mapped_column(String)


class ApprovalRequest(Base):
    __tablename__ = "approval_requests"
    id: Mapped[uuid.UUID] = uuid_pk()
    request_type: Mapped[str] = mapped_column(String, nullable=False)
    entity_type: Mapped[str] = mapped_column(String, nullable=False)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    old_value: Mapped[dict | None] = mapped_column(JSONB)
    new_value: Mapped[dict] = mapped_column(JSONB, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    requested_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    store_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("stores.id"))
    status: Mapped[str] = mapped_column(String, default="pending")
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    review_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    role_code: Mapped[str | None] = mapped_column(String)
    store_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("stores.id"))
    device_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("devices.id"))
    action: Mapped[str] = mapped_column(String, nullable=False)
    entity_type: Mapped[str] = mapped_column(String, nullable=False)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    old_value: Mapped[dict | None] = mapped_column(JSONB)
    new_value: Mapped[dict | None] = mapped_column(JSONB)
    source: Mapped[str] = mapped_column(String, default="api")
    approval_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("approval_requests.id"))
    ip_address: Mapped[str | None] = mapped_column(String)
    reason: Mapped[str | None] = mapped_column(String)
    entity_version: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Vendor(Base):
    __tablename__ = "vendors"
    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String, nullable=False)
    gst_number: Mapped[str | None] = mapped_column(String)
    phone: Mapped[str | None] = mapped_column(String)
    email: Mapped[str | None] = mapped_column(String)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Purchase(Base):
    __tablename__ = "purchases"
    id: Mapped[uuid.UUID] = uuid_pk()
    vendor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("vendors.id"), nullable=False)
    store_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("stores.id"), nullable=False)
    invoice_number: Mapped[str | None] = mapped_column(String)
    invoice_date: Mapped[date | None] = mapped_column(Date)
    total_amount: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    items: Mapped[list["PurchaseItem"]] = relationship(cascade="all, delete-orphan", lazy="selectin")


class PurchaseItem(Base):
    __tablename__ = "purchase_items"
    id: Mapped[uuid.UUID] = uuid_pk()
    purchase_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("purchases.id", ondelete="CASCADE"))
    product_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("products.id"), nullable=False)
    quantity: Mapped[float] = mapped_column(Numeric(12, 3), nullable=False)
    unit_cost: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)


class SyncFailure(Base):
    __tablename__ = "sync_failures"
    id: Mapped[uuid.UUID] = uuid_pk()
    device_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("devices.id"))
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    error: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)


class SyncConflict(Base):
    __tablename__ = "sync_conflicts"
    id: Mapped[uuid.UUID] = uuid_pk()
    device_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("devices.id"))
    entity_type: Mapped[str] = mapped_column(String, nullable=False)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    client_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    server_payload: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ImportBatch(Base):
    __tablename__ = "import_batches"
    id: Mapped[uuid.UUID] = uuid_pk()
    import_type: Mapped[str] = mapped_column(String, nullable=False)
    store_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("stores.id"))
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    file_name: Mapped[str] = mapped_column(String, nullable=False)
    # Every batch keeps its uploaded file (§10) so a "what did we actually
    # import" question can always be answered from the original bytes, not just
    # the parsed staging rows.
    file_bytes: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    status: Mapped[str] = mapped_column(String, default="staged")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    rows: Mapped[list["ImportStagingRow"]] = relationship(cascade="all, delete-orphan", lazy="selectin")


class ImportStagingRow(Base):
    __tablename__ = "import_staging_rows"
    id: Mapped[uuid.UUID] = uuid_pk()
    batch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("import_batches.id", ondelete="CASCADE"))
    line_number: Mapped[int] = mapped_column(nullable=False)
    parsed_values: Mapped[dict] = mapped_column(JSONB, nullable=False)
    computed_action: Mapped[str | None] = mapped_column(String)
    validation_messages: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)


class AiInsightCache(Base):
    __tablename__ = "ai_insight_cache"
    cache_key: Mapped[str] = mapped_column(String, primary_key=True)
    store_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("stores.id"))
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    narrative: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AiCallCounter(Base):
    __tablename__ = "ai_call_counters"
    store_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("stores.id"), primary_key=True)
    call_date: Mapped[date] = mapped_column(Date, primary_key=True)
    call_count: Mapped[int] = mapped_column(default=0)
