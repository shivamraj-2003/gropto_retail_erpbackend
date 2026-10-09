import uuid
from datetime import date, datetime
from typing import Literal, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    """Shared pagination envelope — every list endpoint that can grow
    unbounded over the life of a store (audit log, approvals, attendance,
    orders, ...) returns this instead of a bare list, so the frontend always
    has a real `total` to build page controls from rather than guessing from
    a possibly-truncated page."""
    items: list[T]
    total: int
    limit: int
    offset: int


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    email: str | None = None
    phone: str | None = None
    password: str
    # Login is email+password only. The fingerprint is optional and used solely
    # to rebind this client to an existing Device row (bill numbering, cash
    # sessions and sync are all keyed off it) — omitting it is fine, see
    # auth.py's _default_device_fingerprint.
    device_fingerprint: str | None = None


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class LoginResponse(BaseModel):
    """Superset of TokenResponse: normal logins populate access_token/
    refresh_token exactly as before; a login for an MFA-enabled user instead
    returns mfa_required=True with a short-lived challenge_token, and no
    tokens, until /auth/mfa/login-verify is called with a valid TOTP code."""

    access_token: str | None = None
    refresh_token: str | None = None
    token_type: str = "bearer"
    mfa_required: bool = False
    mfa_challenge_token: str | None = None


class MfaSetupOut(BaseModel):
    secret: str
    provisioning_uri: str


class MfaVerifySetupIn(BaseModel):
    code: str


class MfaLoginVerifyIn(BaseModel):
    challenge_token: str
    code: str


class AuditEntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    user_id: uuid.UUID | None
    role_code: str | None
    store_id: uuid.UUID | None
    device_id: uuid.UUID | None
    ip_address: str | None
    action: str
    entity_type: str
    entity_id: uuid.UUID | None
    entity_version: int | None
    old_value: dict | list | str | None = None
    new_value: dict | list | str | None = None
    reason: str | None
    source: str
    approval_id: uuid.UUID | None
    created_at: datetime


class AuditEntriesPage(BaseModel):
    items: list[AuditEntryOut]
    total: int
    limit: int
    offset: int


class DeviceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    store_id: uuid.UUID
    store_name: str | None = None
    code: str
    fingerprint: str
    status: str
    last_seen_at: datetime | None
    created_at: datetime


class RefreshRequest(BaseModel):
    refresh_token: str
    # Accepted for backwards compatibility and ignored: the refresh token is a
    # bearer secret bound to the user, not to a device. auth.py deliberately
    # does not reject on a fingerprint mismatch.
    device_fingerprint: str | None = None


class ChangePasswordIn(BaseModel):
    current_password: str
    new_password: str = Field(min_length=6)


class ForgotPasswordIn(BaseModel):
    email: str | None = None
    phone: str | None = None
    channel: Literal["auto", "email", "sms", "whatsapp"] = "auto"


class ResetPasswordWithOtpIn(BaseModel):
    email: str | None = None
    phone: str | None = None
    otp: str
    new_password: str = Field(min_length=6)


class ResetPasswordIn(BaseModel):
    new_password: str = Field(min_length=6)


# ---------------------------------------------------------------------------
# User management
# ---------------------------------------------------------------------------

ASSIGNABLE_ROLES = {
    "admin",
    "cashier",
    "store_manager",
    "ceo",
    "coo",
    "finance_head",
    "purchase_head",
    "regional_manager",
    "inventory_user",
    "system_admin",
    "department_head",
    "associate",
    "online_ops",
    "packer",
    "rider",
}


class UserCreateIn(BaseModel):
    email: str
    full_name: str
    phone: str | None = None
    password: str = Field(min_length=6)
    role_code: str
    store_ids: list[uuid.UUID] = Field(default_factory=list)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    email: str | None
    phone: str | None
    full_name: str
    role_code: str
    is_active: bool
    store_ids: list[uuid.UUID]
    created_at: datetime


class UserCreateResult(BaseModel):
    status: str  # "created" | "pending_approval"
    user_id: uuid.UUID | None = None
    request_id: uuid.UUID | None = None


class UserUpdateIn(BaseModel):
    """Both fields optional — send just the one you're changing. Reassigning
    a Cashier/Store Manager to a different store, or changing anyone's role,
    both go through this."""
    role_code: str | None = None
    store_ids: list[uuid.UUID] | None = None


class UserUpdateResult(BaseModel):
    status: str  # "updated" | "pending_approval"
    request_id: uuid.UUID | None = None


class UsersPage(BaseModel):
    items: list[UserOut]
    total: int
    limit: int
    offset: int


class StoreCredentialRow(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    email: str | None
    phone: str | None
    full_name: str
    password_hash: str
    role_code: str
    is_active: bool


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------

class ProductOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    store_quantity: float | None = None
    sku: str
    barcode: str | None
    name: str
    category_id: uuid.UUID | None
    uom: str
    pack_size: float
    purchase_price: float
    selling_price: float
    mrp: float
    tax_rate: float
    hsn_code: str | None
    brand: str | None = None
    # The other barcodes of this product (the main one is `barcode`).
    alt_barcodes: list[str] = []
    is_active: bool
    revision: int

    @field_validator("alt_barcodes", mode="before")
    @classmethod
    def _barcode_strings(cls, value):
        return [getattr(v, "barcode", v) for v in (value or [])]


class ProductCreate(BaseModel):
    sku: str
    barcode: str | None = None
    name: str
    category_id: uuid.UUID | None = None
    uom: str = "EA"
    pack_size: float = 1
    purchase_price: float = 0
    selling_price: float = 0
    mrp: float = 0
    tax_rate: float = 0
    hsn_code: str | None = None
    brand: str | None = None
    extra_barcodes: list[str] = []


class ProductUpdate(BaseModel):
    """Everything about a product except its selling price and MRP (those go through a price change)."""

    name: str | None = None
    barcode: str | None = None
    uom: str | None = None
    pack_size: float | None = None
    purchase_price: float | None = None
    tax_rate: float | None = None
    hsn_code: str | None = None
    brand: str | None = None
    # Replaces the product's other barcodes when sent (omit to leave them as they are).
    extra_barcodes: list[str] | None = None


class ProductPriceChangeRequest(BaseModel):
    selling_price: float | None = None
    mrp: float | None = None
    reason: str | None = None


class ProductPullResponse(BaseModel):
    products: list[ProductOut]
    revision_watermark: int


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------

class StockAdjustmentRequest(BaseModel):
    product_id: uuid.UUID
    store_id: uuid.UUID
    delta: float
    reason_code: str = "adjustment"
    reason: str | None = None


class StockBlockRequest(BaseModel):
    product_id: uuid.UUID
    store_id: uuid.UUID
    quantity: float = Field(gt=0)
    reason: str


class InventoryBalanceOut(BaseModel):
    product_id: uuid.UUID
    store_id: uuid.UUID
    quantity: float
    reserved: float
    in_transit: float
    damaged: float
    blocked: float
    # Point 7 audit fix: "available" previously had to be inferred by the
    # caller (and the whole response was fabricated zeros) — now the real,
    # explicit quantity-minus-unsellable-states figure.
    available: float


# ---------------------------------------------------------------------------
# POS / Sales / Sync push
# ---------------------------------------------------------------------------

class SaleItemIn(BaseModel):
    product_id: uuid.UUID
    product_name_snapshot: str
    tax_rate_snapshot: float
    hsn_code_snapshot: str | None = None
    quantity: float
    unit_price: float
    line_discount: float = 0
    mrp_snapshot: float | None = None


class PaymentIn(BaseModel):
    mode: str
    amount: float
    reference: str | None = None


class RazorpayOrderRequest(BaseModel):
    amount: float
    receipt: str


class RazorpayOrderOut(BaseModel):
    order_id: str
    amount: int
    currency: str
    key_id: str


class RazorpayVerifyRequest(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str


class PaymentConfigOut(BaseModel):
    enabled: bool
    key_id: str | None = None


class SaleIn(BaseModel):
    """One outbox row from the device — the payload for a single bill."""

    id: uuid.UUID  # device-generated UUIDv7
    store_id: uuid.UUID
    device_id: uuid.UUID
    bill_number: str
    cashier_id: uuid.UUID
    customer_phone: str | None = None
    items: list[SaleItemIn]
    payments: list[PaymentIn]
    discount_total: float = 0
    override_user_id: uuid.UUID | None = None
    override_reason: str | None = None
    loyalty_points_redeemed: float = 0
    # Offers and coupon the till applied. Both are part of discount_total; the
    # server re-checks them (services/sync.py) and only the verified share is
    # exempt from the cashier's discount limit.
    offer_discount: float = 0
    coupon_code: str | None = None
    coupon_discount: float = 0
    client_idempotency_key: str
    billed_at: datetime
    # Point 10 audit fix: GST-ready sales data (B2B customer GSTIN) didn't
    # exist. Optional — when absent, the sale is treated as intra-state
    # (unchanged from prior behaviour); the POS UI doesn't collect this yet.
    customer_gstin: str | None = None


class SyncPushBatch(BaseModel):
    device_id: uuid.UUID
    sales: list[SaleIn] = Field(default_factory=list)


class SyncItemVerdict(BaseModel):
    client_id: uuid.UUID
    verdict: str  # applied | duplicate | conflict | rejected
    server_id: uuid.UUID | None = None
    error: str | None = None


class SyncPushResponse(BaseModel):
    results: list[SyncItemVerdict]
    server_revision: int


class SyncPullRequest(BaseModel):
    since_revision: int = 0
    page_size: int = 500


# ---------------------------------------------------------------------------
# Approvals
# ---------------------------------------------------------------------------

class ApprovalDecisionRequest(BaseModel):
    approve: bool
    note: str | None = None


class ApprovalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    request_type: str
    entity_type: str
    entity_id: uuid.UUID | None
    old_value: dict | None
    new_value: dict
    reason: str | None
    requested_by: uuid.UUID
    store_id: uuid.UUID | None
    status: str
    reviewed_by: uuid.UUID | None
    review_note: str | None
    created_at: datetime
    reviewed_at: datetime | None
    required_levels: int = 1
    approved_levels: int = 0


class ApprovalStepOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    level: int
    approver_id: uuid.UUID
    approver_role_code: str | None
    decision: str
    note: str | None
    created_at: datetime


# ---------------------------------------------------------------------------
# Vendors / Purchases
# ---------------------------------------------------------------------------

class VendorCreate(BaseModel):
    name: str
    gst_number: str | None = None
    phone: str | None = None
    email: str | None = None
    category: str | None = None
    service_area: str | None = None
    credit_days: int = 30
    bank_account_name: str | None = None
    bank_account_number: str | None = None
    bank_ifsc: str | None = None
    bank_name: str | None = None


class VendorOut(VendorCreate):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    is_active: bool


class VendorUpdateIn(BaseModel):
    """All optional — send just what's changing. Point 6 audit fix: there
    was previously no way to ever edit or deactivate a vendor after creation."""
    name: str | None = None
    gst_number: str | None = None
    phone: str | None = None
    email: str | None = None
    category: str | None = None
    service_area: str | None = None
    credit_days: int | None = None
    bank_account_name: str | None = None
    bank_account_number: str | None = None
    bank_ifsc: str | None = None
    bank_name: str | None = None
    is_active: bool | None = None


class PurchaseItemIn(BaseModel):
    product_id: uuid.UUID
    quantity: float
    unit_cost: float


class PurchaseCreate(BaseModel):
    vendor_id: uuid.UUID
    store_id: uuid.UUID
    invoice_number: str | None = None
    invoice_date: date | None = None
    items: list[PurchaseItemIn]


class PurchaseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    vendor_id: uuid.UUID
    store_id: uuid.UUID
    invoice_number: str | None
    invoice_date: date | None
    total_amount: float


# ---------------------------------------------------------------------------
# AI dashboard
# ---------------------------------------------------------------------------

class AiInsightOut(BaseModel):
    facts: dict
    narrative: str | None
    generated_at: datetime
    from_cache: bool
