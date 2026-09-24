import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    email: str | None = None
    phone: str | None = None
    password: str
    device_fingerprint: str
    device_activation_code: str | None = None


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshRequest(BaseModel):
    refresh_token: str
    device_fingerprint: str


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------

class ProductOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
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
    is_active: bool
    revision: int


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


class InventoryBalanceOut(BaseModel):
    product_id: uuid.UUID
    store_id: uuid.UUID
    quantity: float
    reserved: float
    in_transit: float
    damaged: float
    blocked: float


# ---------------------------------------------------------------------------
# POS / Sales / Sync push
# ---------------------------------------------------------------------------

class SaleItemIn(BaseModel):
    product_id: uuid.UUID
    product_name_snapshot: str
    tax_rate_snapshot: float
    quantity: float
    unit_price: float
    line_discount: float = 0


class PaymentIn(BaseModel):
    mode: str
    amount: float
    reference: str | None = None


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
    client_idempotency_key: str
    billed_at: datetime


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


# ---------------------------------------------------------------------------
# Vendors / Purchases
# ---------------------------------------------------------------------------

class VendorCreate(BaseModel):
    name: str
    gst_number: str | None = None
    phone: str | None = None
    email: str | None = None


class VendorOut(VendorCreate):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    is_active: bool


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
