"""GST determination: place of supply, inter-state tax split, turnover
aggregation, and e-invoice applicability (Point 10 audit fix — none of this
existed; GST handling was intra-state-only with no way to ever compute it
correctly otherwise).

Applicability and turnover are deliberately never judged per Store: e-
invoicing's AATO (Aggregate Annual Turnover) threshold is a PAN-wide
regulatory fact, so turnover is summed over every store of every company
sharing a PAN, and a single store's revenue is not a valid proxy for it.
"""

import uuid
from datetime import date, datetime, timezone

from sqlalchemy import and_, func, select
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


#: E-invoicing threshold: Aggregate Annual Turnover above ₹5 crore (CBIC
#: notification, in force since 1 Aug 2023). Used when a company has no
#: threshold of its own; a company's aato_threshold overrides it.
DEFAULT_AATO_THRESHOLD = 5_00_00_000.0


def current_financial_year(today: date | None = None) -> int:
    today = today or date.today()
    return today.year if today.month >= 4 else today.year - 1


async def _group_company_ids(db: AsyncSession, company: Company) -> list[uuid.UUID]:
    """Every company the threshold is measured across. AATO is a PAN-wide
    figure — all GSTINs (one per state) under the same PAN add up — so when
    the company has a PAN, its siblings count too. Without a PAN the company
    stands alone."""
    if not company.pan:
        return [company.id]
    ids = (
        await db.execute(select(Company.id).where(func.upper(Company.pan) == company.pan.strip().upper()))
    ).scalars().all()
    return list(ids) or [company.id]


async def turnover_by_store(db: AsyncSession, *, company: Company, financial_year: int) -> list[dict]:
    """Completed-sale turnover for the financial year, one row per store, for
    every store under the company's PAN group (Indian FY: 1 Apr -> 31 Mar)."""
    fy_start = datetime(financial_year, 4, 1, tzinfo=timezone.utc)
    fy_end = datetime(financial_year + 1, 4, 1, tzinfo=timezone.utc)  # exclusive: includes all of 31 Mar
    company_ids = await _group_company_ids(db, company)
    rows = (
        await db.execute(
            select(Store.id, Store.code, Store.name, Store.company_id, func.coalesce(func.sum(Sale.grand_total), 0))
            .select_from(Store)
            .outerjoin(
                Sale,
                and_(
                    Sale.store_id == Store.id,
                    Sale.status == "completed",
                    Sale.billed_at >= fy_start,
                    Sale.billed_at < fy_end,
                ),
            )
            .where(Store.company_id.in_(company_ids))
            .group_by(Store.id, Store.code, Store.name, Store.company_id)
            .order_by(Store.code)
        )
    ).all()
    return [
        {"store_id": str(sid), "code": code, "name": name, "company_id": str(cid), "turnover": round(float(total), 2)}
        for sid, code, name, cid, total in rows
    ]


async def compute_turnover(db: AsyncSession, *, company: Company, financial_year: int) -> float:
    """Aggregate Annual Turnover across every store of every company under
    the same PAN — never one store, never one GSTIN in isolation."""
    stores = await turnover_by_store(db, company=company, financial_year=financial_year)
    return round(sum(s["turnover"] for s in stores), 2)


def effective_threshold(company: Company) -> float:
    return float(company.aato_threshold) if company.aato_threshold is not None else DEFAULT_AATO_THRESHOLD


async def is_einvoice_required(db: AsyncSession, *, company: Company, as_of: date | None = None) -> bool:
    """E-invoicing is mandatory once AATO exceeded the threshold in any
    *preceding* financial year, so it is judged on last year's PAN-wide
    turnover — or because Finance flagged the company explicitly (e.g. an
    entity that opted in, or crossed in an earlier year)."""
    if company.einvoice_applicable:
        return True
    fy = current_financial_year(as_of)
    return await compute_turnover(db, company=company, financial_year=fy - 1) > effective_threshold(company)


async def check_einvoice_applicability(db: AsyncSession, *, company: Company, financial_year: int) -> dict:
    """Applicability for one company, measured across its whole PAN group.

    * einvoice_applicable — the flag Finance set on this company.
    * einvoice_required — what actually applies: the flag, or last FY's
      PAN-wide turnover above the threshold (default ₹5 crore).
    * stores — the per-store turnover that adds up to the total, so it's
      clear the threshold is judged on the sum, not on any one store."""
    stores = await turnover_by_store(db, company=company, financial_year=financial_year)
    turnover = round(sum(s["turnover"] for s in stores), 2)
    previous = await compute_turnover(db, company=company, financial_year=financial_year - 1)
    threshold = effective_threshold(company)
    group_ids = await _group_company_ids(db, company)
    group_names = (
        await db.execute(select(Company.name, Company.gstin).where(Company.id.in_(group_ids)).order_by(Company.name))
    ).all()

    required = bool(company.einvoice_applicable) or previous > threshold
    warning = None
    if not required and turnover > threshold:
        warning = (
            f"Turnover across all stores under this PAN is ₹{turnover:,.2f} this year, above the "
            f"₹{threshold:,.0f} threshold — e-invoicing becomes mandatory from next financial year. "
            "Set the company as e-invoice applicable earlier if you want to start now."
        )
    elif required and not company.einvoice_applicable:
        warning = (
            f"Last year's turnover across all stores under this PAN (₹{previous:,.2f}) exceeded "
            f"₹{threshold:,.0f}, so e-invoicing applies to every GSTIN under it."
        )
    if not stores:
        warning = "No stores are linked to this company yet — link stores to it (Stores → Parent Company) so their sales count."
    return {
        "company_id": str(company.id),
        "gstin": company.gstin,
        "pan": company.pan,
        "financial_year": financial_year,
        "turnover": turnover,
        "previous_year_turnover": previous,
        "aato_threshold": float(company.aato_threshold) if company.aato_threshold is not None else None,
        "effective_threshold": threshold,
        "einvoice_applicable": company.einvoice_applicable,
        "einvoice_required": required,
        "scope": "PAN" if company.pan else "Company",
        "companies_in_scope": [{"name": n, "gstin": g} for n, g in group_names],
        "stores": stores,
        "warning": warning,
    }
