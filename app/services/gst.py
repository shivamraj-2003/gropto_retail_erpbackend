"""GST determination: place of supply, inter-state tax split, turnover
aggregation, and e-invoice applicability (Point 10 audit fix — none of this
existed; GST handling was intra-state-only with no way to ever compute it
correctly otherwise).

Applicability and turnover are deliberately modelled at the Company (GSTIN/
PAN-bearing legal entity) level, never at the individual Store level — e-
invoicing's AATO (Aggregate Annual Turnover) threshold is a PAN-wide
regulatory fact, and a single store's revenue is not a valid proxy for it.
"""

import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import Sale, Store
from app.models.models_phase4 import Company

# Real GST state codes (first two digits of any GSTIN), per CBIC's published
# list — used only to compare the selling store's state against a supplied
# customer GSTIN's state prefix, never to fabricate a GSTIN.
GST_STATE_CODES: dict[str, str] = {
    "01": "Jammu and Kashmir", "02": "Himachal Pradesh", "03": "Punjab", "04": "Chandigarh",
    "05": "Uttarakhand", "06": "Haryana", "07": "Delhi", "08": "Rajasthan", "09": "Uttar Pradesh",
    "10": "Bihar", "11": "Sikkim", "12": "Arunachal Pradesh", "13": "Nagaland", "14": "Manipur",
    "15": "Mizoram", "16": "Tripura", "17": "Meghalaya", "18": "Assam", "19": "West Bengal",
    "20": "Jharkhand", "21": "Odisha", "22": "Chhattisgarh", "23": "Madhya Pradesh",
    "24": "Gujarat", "26": "Dadra and Nagar Haveli and Daman and Diu", "27": "Maharashtra",
    "29": "Karnataka", "30": "Goa", "31": "Lakshadweep", "32": "Kerala", "33": "Tamil Nadu",
    "34": "Puducherry", "35": "Andaman and Nicobar Islands", "36": "Telangana",
    "37": "Andhra Pradesh", "38": "Ladakh",
}
STATE_TO_GST_CODE = {name.lower(): code for code, name in GST_STATE_CODES.items()}


def gstin_state(gstin: str | None) -> str | None:
    """Returns the state name for a GSTIN's first-two-digit state code, or
    None if the GSTIN doesn't look like one (no fabricated fallback)."""
    if not gstin or len(gstin) < 2 or not gstin[:2].isdigit():
        return None
    return GST_STATE_CODES.get(gstin[:2])


def determine_place_of_supply(*, store_state: str | None, customer_gstin: str | None) -> tuple[str | None, bool]:
    """Returns (place_of_supply, is_inter_state). Defaults to the selling
    store's own state (the overwhelmingly common in-store/walk-in case) —
    only flips to inter-state when a customer GSTIN is actually supplied and
    its state prefix genuinely differs from the store's."""
    customer_state = gstin_state(customer_gstin)
    if customer_state and store_state and customer_state.lower() != store_state.lower():
        return customer_state, True
    return store_state, False


def split_tax(line_net: float, rate: float, *, inter_state: bool) -> tuple[float, float, float, float]:
    """Returns (taxable_value, cgst, sgst, igst) for one line, GST-inclusive
    pricing backed out exactly like sync.py already does for CGST+SGST."""
    taxable = line_net / (1 + rate / 100) if rate else line_net
    tax = line_net - taxable
    if inter_state:
        return taxable, 0.0, 0.0, tax
    return taxable, tax / 2, tax / 2, 0.0


async def compute_turnover(db: AsyncSession, *, company_id: uuid.UUID, financial_year: int) -> float:
    """Aggregate Annual Turnover for one GSTIN/company, across every store
    linked to it — the correct grouping level per §15 of this audit, not
    per-store. Indian FY runs Apr 1 -> Mar 31."""
    fy_start = date(financial_year, 4, 1)
    fy_end = date(financial_year + 1, 3, 31)
    store_ids = (
        await db.execute(select(Store.id).where(Store.company_id == company_id))
    ).scalars().all()
    if not store_ids:
        return 0.0
    total = (
        await db.execute(
            select(Sale.grand_total).where(
                Sale.store_id.in_(store_ids),
                Sale.status == "completed",
                Sale.billed_at >= fy_start,
                Sale.billed_at <= fy_end,
            )
        )
    ).scalars().all()
    return round(float(sum(total)), 2)


async def check_einvoice_applicability(db: AsyncSession, *, company: Company, financial_year: int) -> dict:
    """Reports applicability — never auto-derives it from a hardcoded
    threshold. Company.einvoice_applicable is the actual regulatory fact,
    set explicitly by Finance; aato_threshold (if configured) only drives an
    informational warning once turnover crosses it, so Finance knows to
    re-check the determination — it never flips einvoice_applicable itself."""
    turnover = await compute_turnover(db, company_id=company.id, financial_year=financial_year)
    warning = None
    if company.aato_threshold is not None and turnover >= float(company.aato_threshold) and not company.einvoice_applicable:
        warning = (
            f"Turnover ₹{turnover:,.2f} has crossed the configured AATO threshold "
            f"₹{float(company.aato_threshold):,.2f} — confirm e-invoice applicability with Finance."
        )
    return {
        "company_id": str(company.id),
        "gstin": company.gstin,
        "pan": company.pan,
        "financial_year": financial_year,
        "turnover": turnover,
        "aato_threshold": float(company.aato_threshold) if company.aato_threshold is not None else None,
        "einvoice_applicable": company.einvoice_applicable,
        "warning": warning,
    }
