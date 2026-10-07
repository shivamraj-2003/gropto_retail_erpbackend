"""E-invoice / IRN / IRP integration skeleton (Point 10 audit fix).

This did not exist in any form before. What's real here: applicability
checking (services/gst.py), a compliant GST e-invoice payload (the actual
IRP JSON schema shape — TranDtls/DocDtls/SellerDtls/BuyerDtls/ItemList/
ValDtls), a status machine, and idempotent submission bookkeeping.

What's intentionally NOT real: an actual network call to an IRP. That
requires a live GSP (GST Suvidha Provider) account — real credentials, a
registered API consumer, and (for production use) a digital signature
certificate — none of which exist in this environment. `is_configured()`
gates it exactly like services/payments.py gates Razorpay: when unset,
submission is refused with a clear, auditable "not configured" outcome
instead of a fabricated IRN. Dropping in a real GSP client later only means
replacing `_call_irp()`'s body — every surrounding piece (applicability,
payload, status, retry bookkeeping, audit) is already correct.
"""

from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser
from app.core.config import settings
from app.models.models import Sale, Store
from app.models.models_phase4 import Company, EInvoice
from app.services.audit import write_audit
from app.services.gst import GST_STATE_CODES, is_einvoice_required_for_store

MAX_RETRY_COUNT = 5


def is_configured() -> bool:
    return bool(settings.gsp_api_base_url and settings.gsp_client_id and settings.gsp_client_secret)


class NotConfigured(Exception):
    pass


def build_payload(*, sale: Sale, store: Store, company: Company | None) -> dict:
    """Real GST e-invoice schema shape (IRP schema v1.1 field names) built
    from actual transaction data. Not submitted anywhere in this pass — see
    module docstring."""
    state_code = next((code for code, name in GST_STATE_CODES.items() if name.lower() == (store.state or "").lower()), None)
    items = []
    for idx, item in enumerate(sale.items, start=1):
        items.append(
            {
                "SlNo": str(idx),
                "PrdDesc": item.product_name_snapshot,
                "HsnCd": item.hsn_code_snapshot or "",
                "Qty": float(item.quantity),
                "UnitPrice": float(item.unit_price),
                "TotAmt": float(item.quantity) * float(item.unit_price),
                "Discount": float(item.line_discount),
                "AssAmt": float(item.taxable_value),
                "GstRt": float(item.tax_rate_snapshot),
                "CgstAmt": float(item.cgst_amount),
                "SgstAmt": float(item.sgst_amount),
                "IgstAmt": float(item.igst_amount),
                "TotItemVal": float(item.line_total),
            }
        )
    return {
        "Version": "1.1",
        "TranDtls": {"TaxSch": "GST", "SupTyp": "B2B" if sale.customer_gstin else "B2C"},
        "DocDtls": {
            "Typ": {"invoice": "INV", "credit_note": "CRN", "debit_note": "DBN"}.get(sale.document_type, "INV"),
            "No": sale.bill_number,
            "Dt": sale.billed_at.strftime("%d/%m/%Y"),
        },
        "SellerDtls": {
            "Gstin": store.gstin or (company.gstin if company else None),
            "LglNm": company.legal_entity_name if company else store.name,
            "Loc": store.city,
            "Stcd": state_code,
        },
        "BuyerDtls": {
            "Gstin": sale.customer_gstin,
            "Pos": next((code for code, name in GST_STATE_CODES.items() if name == sale.place_of_supply), state_code),
        },
        "ItemList": items,
        "ValDtls": {
            "AssVal": float(sale.subtotal),
            "CgstVal": sum(float(i.cgst_amount) for i in sale.items),
            "SgstVal": sum(float(i.sgst_amount) for i in sale.items),
            "IgstVal": sum(float(i.igst_amount) for i in sale.items),
            "TotInvVal": float(sale.grand_total),
        },
    }


async def _call_irp(payload: dict) -> dict:
    """The actual network call to the GSP/IRP. Real credentials don't exist
    in this environment, so this always refuses rather than fabricating a
    response — see module docstring."""
    raise NotConfigured("GSP/IRP credentials are not configured (settings.gsp_api_base_url/client_id/client_secret)")


async def get_or_create_einvoice(db: AsyncSession, *, sale: Sale, store: Store, company: Company | None) -> EInvoice:
    result = await db.execute(select(EInvoice).where(EInvoice.sale_id == sale.id))
    existing = result.scalar_one_or_none()
    if existing is not None:
        return existing
    # Judged on this store's own sales — each store files under its own GSTIN.
    required = await is_einvoice_required_for_store(db, store=store, as_of=sale.billed_at.date() if sale.billed_at else None)
    einvoice = EInvoice(
        sale_id=sale.id,
        company_id=company.id if company else None,
        status="pending" if required else "not_applicable",
    )
    db.add(einvoice)
    await db.flush()
    return einvoice


async def submit_einvoice(
    db: AsyncSession, *, current: CurrentUser | None, sale: Sale, store: Store, company: Company | None
) -> EInvoice:
    """current=None marks a system/scheduler-triggered retry (audited with no
    user attribution, same convention as scheduler.py's other jobs)."""
    user_id = current.user_id if current else None
    role_code = current.role_code if current else None

    einvoice = await get_or_create_einvoice(db, sale=sale, store=store, company=company)

    if einvoice.status == "irn_generated":
        return einvoice  # idempotent: already succeeded, never resubmit
    if not (store.gstin or (company.gstin if company else None)):
        raise HTTPException(
            status_code=409,
            detail="This store has no GSTIN, so an e-invoice can't be raised. Add the store's GSTIN in Stores first.",
        )
    if einvoice.status == "not_applicable":
        raise HTTPException(
            status_code=409,
            detail="E-invoicing doesn't apply to this store yet: its own sales haven't passed the limit "
            "(₹5 crore by default) and it isn't switched on in Stores.",
        )
    if einvoice.status == "cancelled":
        raise HTTPException(status_code=409, detail="This e-invoice was cancelled; cannot resubmit the same sale")

    payload = build_payload(sale=sale, store=store, company=company)
    einvoice.payload_json = payload
    einvoice.status = "submitted"
    await write_audit(
        db,
        user_id=user_id,
        role_code=role_code,
        store_id=sale.store_id,
        device_id=None,
        action="einvoice.submission_attempted",
        entity_type="einvoice",
        entity_id=einvoice.id,
        new_value={"sale_id": str(sale.id), "idempotency_key": str(einvoice.idempotency_key)},
    )

    if not is_configured():
        einvoice.status = "failed"
        einvoice.error_response = "GSP/IRP not configured — see settings.gsp_api_base_url/client_id/client_secret"
        einvoice.retry_count += 1
        await write_audit(
            db,
            user_id=user_id,
            role_code=role_code,
            store_id=sale.store_id,
            device_id=None,
            action="einvoice.submission_failed",
            entity_type="einvoice",
            entity_id=einvoice.id,
            new_value={"reason": einvoice.error_response},
        )
        return einvoice

    try:
        response = await _call_irp(payload)
        einvoice.irn = response.get("Irn")
        einvoice.ack_no = response.get("AckNo")
        ack_dt = response.get("AckDt")
        einvoice.ack_date = datetime.fromisoformat(ack_dt) if ack_dt else datetime.now(timezone.utc)
        einvoice.signed_qr_code = response.get("SignedQRCode")
        einvoice.status = "irn_generated"
        await write_audit(
            db,
            user_id=user_id,
            role_code=role_code,
            store_id=sale.store_id,
            device_id=None,
            action="einvoice.irn_generated",
            entity_type="einvoice",
            entity_id=einvoice.id,
            new_value={"irn": einvoice.irn, "ack_no": einvoice.ack_no},
        )
    except NotConfigured as exc:
        einvoice.status = "failed"
        einvoice.error_response = str(exc)
        einvoice.retry_count += 1
        await write_audit(
            db,
            user_id=user_id,
            role_code=role_code,
            store_id=sale.store_id,
            device_id=None,
            action="einvoice.submission_failed",
            entity_type="einvoice",
            entity_id=einvoice.id,
            new_value={"reason": str(exc)},
        )
    return einvoice


async def cancel_einvoice(db: AsyncSession, *, current: CurrentUser, einvoice: EInvoice, reason: str) -> EInvoice:
    if einvoice.status != "irn_generated":
        raise HTTPException(status_code=409, detail=f"Cannot cancel an e-invoice in status {einvoice.status}")
    einvoice.status = "cancelled"
    einvoice.cancelled_reason = reason
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=None,
        device_id=None,
        action="einvoice.cancelled",
        entity_type="einvoice",
        entity_id=einvoice.id,
        reason=reason,
    )
    return einvoice


async def retry_failed_einvoices(db: AsyncSession) -> int:
    """Scheduler job hook: picks up `failed` rows under the retry cap and
    re-attempts. Always a safe no-op when GSP isn't configured (matches
    submit_einvoice's own behaviour) — never spins indefinitely."""
    if not is_configured():
        return 0
    rows = (
        await db.execute(
            select(EInvoice).where(EInvoice.status.in_(("failed", "pending")), EInvoice.retry_count < MAX_RETRY_COUNT)
        )
    ).scalars().all()
    retried = 0
    for einvoice in rows:
        sale = await db.get(Sale, einvoice.sale_id)
        if sale is None:
            continue
        store = await db.get(Store, sale.store_id)
        company = await db.get(Company, einvoice.company_id) if einvoice.company_id else None
        await submit_einvoice(db, current=None, sale=sale, store=store, company=company)
        retried += 1
    return retried


QUEUE_WINDOW_DAYS = 30


async def queue_required_einvoices(db: AsyncSession) -> int:
    """Per store, once e-invoicing applies to it: every B2B invoice (one with a
    customer GSTIN) from the last 30 days gets an e-invoice record in
    `pending`, ready to go to the government portal. Walk-in B2C sales are not
    reported invoice by invoice, so they are left out. Safe to run repeatedly:
    a sale is only ever queued once."""
    from datetime import timedelta

    stores = (await db.execute(select(Store).where(Store.is_active.is_(True)))).scalars().all()
    since = datetime.now(timezone.utc) - timedelta(days=QUEUE_WINDOW_DAYS)
    queued = 0
    for store in stores:
        if not await is_einvoice_required_for_store(db, store=store):
            continue
        sales = (
            await db.execute(
                select(Sale)
                .outerjoin(EInvoice, EInvoice.sale_id == Sale.id)
                .where(
                    Sale.store_id == store.id,
                    Sale.status == "completed",
                    Sale.customer_gstin.is_not(None),
                    Sale.billed_at >= since,
                    EInvoice.id.is_(None),
                )
            )
        ).scalars().all()
        for sale in sales:
            db.add(EInvoice(sale_id=sale.id, company_id=store.company_id, status="pending"))
            queued += 1
    await db.flush()
    return queued
