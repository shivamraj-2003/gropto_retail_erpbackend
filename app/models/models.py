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


class Region(Base):
    """Real Company→City/Cluster→Store rollup layer (blueprint §1/§2) — replaces
    the free-text-only Store.cluster tag with a queryable hierarchy."""

    __tablename__ = "regions"
    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String, nullable=False)
    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    # Point 15 audit fix: Region had a model and a live FK from Store.region_id
    # but no API/UI anywhere — a dead table nothing could create a row in
    # except a seed script. is_active added for parity with every other
    # Point-15 catalogue entity's deactivate support.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Store(Base):
    __tablename__ = "stores"
    id: Mapped[uuid.UUID] = uuid_pk()
    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    city: Mapped[str | None] = mapped_column(String)
    cluster: Mapped[str | None] = mapped_column(String)
    region_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("regions.id"))
    # Real FK counterpart to clusters.regional_manager_id (Point 2 audit fix —
    # previously no Store ever pointed at a Cluster, so a manager's cluster
    # assignment carried zero actual stores). `cluster` above stays as the
    # pre-existing free-text tag, untouched, to avoid breaking anything that
    # already reads it.
    cluster_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("clusters.id"))
    # Point 3 audit fix: blueprint's Sales/Sq Ft and Target Achievement KPIs
    # had no backing columns anywhere — both NULL by default (no fabricated
    # defaults), set per store by whoever owns store setup.
    area_sqft: Mapped[float | None] = mapped_column(Numeric(10, 2))
    target_revenue_monthly: Mapped[float | None] = mapped_column(Numeric(14, 2))
    # Point 10 audit fix: GST registration/e-invoice applicability attaches to
    # a legal-entity GSTIN (companies.gstin), not to a store in isolation —
    # company_id is the real link that lets turnover/applicability be
    # computed correctly across every store under one PAN. gstin here covers
    # the (less common) case of a store having its own branch-level
    # registration distinct from the parent company's; state drives
    # place-of-supply / inter-state tax determination.
    company_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("companies.id"))
    gstin: Mapped[str | None] = mapped_column(String)
    state: Mapped[str | None] = mapped_column(String)
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
    # Point 14: custom roles. is_system marks the seeded blueprint roles (they
    # can be edited but never deleted); scope_level 'global' = sees every store
    # unless narrowed by a company scope, 'assigned' = only explicitly scoped
    # stores/clusters/companies (replaces the hardcoded ENTERPRISE_WIDE_ROLES).
    description: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    scope_level: Mapped[str] = mapped_column(String, default="assigned", server_default="assigned")
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Permission(Base):
    __tablename__ = "permissions"
    id: Mapped[uuid.UUID] = uuid_pk()
    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    # Point 14 catalogue metadata (app/core/permission_catalog.py). Pre-Point-14
    # coarse codes are kept, flagged is_deprecated, so existing grants survive
    # but the admin matrix no longer offers them.
    module: Mapped[str | None] = mapped_column(String)
    feature: Mapped[str | None] = mapped_column(String)
    action: Mapped[str | None] = mapped_column(String)
    description: Mapped[str | None] = mapped_column(Text)
    is_deprecated: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))


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
    mfa_secret: Mapped[str | None] = mapped_column(String)
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # Point 14: full-authority override independent of role. Only an existing
    # Super Admin can set it (rbac.py); the `super_admin` role code implies it.
    is_super_admin: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    role: Mapped["Role"] = relationship(lazy="joined")


class UserStore(Base):
    __tablename__ = "user_stores"
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), primary_key=True)
    store_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("stores.id"), primary_key=True)


class UserRole(Base):
    """Point 14: additional roles beyond users.role_id (the primary role, which
    still drives approval/discount behaviour). Effective permissions are the
    union over the primary and every active additional role."""

    __tablename__ = "user_roles"
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), primary_key=True)
    role_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("roles.id"), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UserScope(Base):
    """Point 14: WHERE a user may act, beyond direct store assignment
    (user_stores). company/region/cluster/city expand to their stores;
    warehouse and department narrow WMS and HR respectively."""

    __tablename__ = "user_scopes"
    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    scope_type: Mapped[str] = mapped_column(String, nullable=False)  # company|region|cluster|city|warehouse|department
    scope_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    scope_value: Mapped[str | None] = mapped_column(String)  # city name (cities aren't a table)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ApprovalRule(Base):
    """Point 14 approval matrix. For a request_type (optionally an amount band):
    whether it needs approval at all (threshold), who may approve, how many
    distinct approvals, and whether the maker may be the checker."""

    __tablename__ = "approval_rules"
    id: Mapped[uuid.UUID] = uuid_pk()
    request_type: Mapped[str] = mapped_column(String, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    # Amounts at or below this apply without approval (only meaningful for the
    # request types whose service checks a threshold); NULL = always approve.
    threshold_amount: Mapped[float | None] = mapped_column(Numeric(14, 2))
    min_amount: Mapped[float | None] = mapped_column(Numeric(14, 2))
    max_amount: Mapped[float | None] = mapped_column(Numeric(14, 2))
    approver_permission: Mapped[str] = mapped_column(String, default="approval.request.approve", server_default="approval.request.approve")
    approver_role_codes: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    levels: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    maker_checker: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    conditions: Mapped[dict | None] = mapped_column(JSONB)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ApprovalStep(Base):
    """One approver's decision on one level of an approval request — the
    approval history, and the record multi-level approval counts against."""

    __tablename__ = "approval_steps"
    id: Mapped[uuid.UUID] = uuid_pk()
    request_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("approval_requests.id"), nullable=False, index=True)
    level: Mapped[int] = mapped_column(Integer, nullable=False)
    approver_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    approver_role_code: Mapped[str | None] = mapped_column(String)
    decision: Mapped[str] = mapped_column(String, nullable=False)  # approved|rejected
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Device(Base):
    __tablename__ = "devices"
    id: Mapped[uuid.UUID] = uuid_pk()
    store_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("stores.id"), nullable=False)
    code: Mapped[str] = mapped_column(String, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String, default="active")
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


class PasswordResetOtp(Base):
    __tablename__ = "password_reset_otps"
    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    code_hash: Mapped[str] = mapped_column(String, nullable=False)
    used: Mapped[bool] = mapped_column(Boolean, default=False)
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
    # GST rate as a percentage (e.g. 18 for 18%). selling_price/mrp are
    # GST-INCLUSIVE — per Indian law the MRP printed on a product already
    # includes tax, so billing backs the tax out of this price rather than
    # adding it on top. See app/services/sync.py's process_sale for the split.
    tax_rate: Mapped[float] = mapped_column(Numeric(5, 2), default=0)
    hsn_code: Mapped[str | None] = mapped_column(String)
    brand: Mapped[str | None] = mapped_column(String)
    is_private_label: Mapped[bool] = mapped_column(Boolean, default=False)
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
    # Point 11 audit fix: the email campaign channel had no field to send
    # to at all — Customer had no email address anywhere. push_token is the
    # device token a push provider (FCM) actually sends to; nothing in this
    # app registers one yet (no mobile/web push SDK integration exists), so
    # it stays null until that registration flow is built — the push send
    # path reports "no device token on file" rather than silently skipping.
    email: Mapped[str | None] = mapped_column(String)
    push_token: Mapped[str | None] = mapped_column(String)
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
    # Point 9 audit fix: points never expired under any code path. Null/0 =
    # never expire (opt-in), preserving existing behaviour by default.
    points_expiry_days: Mapped[int | None] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LoyaltyTier(Base):
    """Blueprint §9: "Loyalty points earn/burn, tiers, expiry." Tier is derived
    from lifetime points earned (sum of positive loyalty_ledger deltas), not
    stored per-customer — a customer's tier is always recomputed from their
    real ledger history rather than a cached, driftable field."""

    __tablename__ = "loyalty_tiers"
    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String, nullable=False)  # e.g. Silver, Gold, Platinum
    min_lifetime_points: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    earn_rate_multiplier: Mapped[float] = mapped_column(Numeric(6, 4), default=1.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


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
    # Point 10 audit fix: GSTR-1-ready sales data didn't exist — no place of
    # supply, no document type, no customer GSTIN for B2B bills. Defaulted
    # server-side at sync time (services/sync.py), not collected from the
    # POS UI (a live-till flow this pass doesn't touch — see services/gst.py
    # for the intra-state-by-default assumption and how a supplied
    # customer_gstin can flip place_of_supply/tax split to inter-state).
    place_of_supply: Mapped[str | None] = mapped_column(String)
    document_type: Mapped[str] = mapped_column(String, default="invoice")  # invoice, credit_note, debit_note
    customer_gstin: Mapped[str | None] = mapped_column(String)
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
    hsn_code_snapshot: Mapped[str | None] = mapped_column(String)
    quantity: Mapped[float] = mapped_column(Numeric(12, 3), nullable=False)
    unit_price: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    line_discount: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    line_total: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    # Taxable value (line_total with GST backed out) and the GST amount,
    # split evenly into CGST+SGST — every walk-in POS sale is intra-state
    # (the customer is standing in the store), so IGST never applies here.
    taxable_value: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    cgst_amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    sgst_amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    # Point 10 audit fix: no IGST column existed anywhere in the schema — an
    # inter-state sale (customer_gstin's state differing from the selling
    # store's state) could never be taxed correctly. 0 for every existing
    # intra-state sale; only non-zero when services/gst.py determines the
    # sale is inter-state.
    igst_amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    # Point 4 audit fix: MRP was never captured on the sale itself — only the
    # selling price. Snapshotted at billing time (like product_name_snapshot)
    # so it survives later MRP changes to the product catalogue.
    mrp_snapshot: Mapped[float | None] = mapped_column(Numeric(12, 2))


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
    # Point 14 multi-level approval: how many distinct approvals the matching
    # ApprovalRule demands, and how many have been recorded so far.
    required_levels: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    approved_levels: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
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
    # Point 6 audit fix: Vendor used to be name/gst/phone/email/is_active
    # only — no bank details, category, terms, or service area, and no
    # update/deactivate path existed at all.
    gst_number: Mapped[str | None] = mapped_column(String, unique=True)
    phone: Mapped[str | None] = mapped_column(String)
    email: Mapped[str | None] = mapped_column(String)
    category: Mapped[str | None] = mapped_column(String)
    service_area: Mapped[str | None] = mapped_column(String)
    credit_days: Mapped[int] = mapped_column(Integer, default=30)
    bank_account_name: Mapped[str | None] = mapped_column(String)
    bank_account_number: Mapped[str | None] = mapped_column(String)
    bank_ifsc: Mapped[str | None] = mapped_column(String)
    bank_name: Mapped[str | None] = mapped_column(String)
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
