import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_any_permission, require_permission, require_store_access
from app.core.database import get_db
from app.models.models_phase2 import BillSettings
from app.services.audit import write_audit

router = APIRouter(prefix="/bill-settings", tags=["bill-settings"])

FIELDS = (
    "heading", "brand_name", "legal_name", "address", "gstin", "fssai_no", "cin_no", "phone", "website",
    "footer_text", "terms_text", "sign_off", "thanks_text",
)


class BillSettingsIn(BaseModel):
    heading: str | None = Field(default=None, max_length=80)
    brand_name: str | None = Field(default=None, max_length=80)
    legal_name: str | None = Field(default=None, max_length=120)
    address: str | None = Field(default=None, max_length=300)
    gstin: str | None = Field(default=None, max_length=20)
    fssai_no: str | None = Field(default=None, max_length=30)
    cin_no: str | None = Field(default=None, max_length=30)
    phone: str | None = Field(default=None, max_length=30)
    website: str | None = Field(default=None, max_length=120)
    footer_text: str | None = Field(default=None, max_length=600)
    terms_text: str | None = Field(default=None, max_length=800)
    sign_off: str | None = Field(default=None, max_length=80)
    thanks_text: str | None = Field(default=None, max_length=80)


def _out(row: BillSettings | None, source: str) -> dict:
    data = {f: (getattr(row, f) or "") if row else "" for f in FIELDS}
    return {**data, "source": source}


async def _rows(db: AsyncSession, store_id: uuid.UUID | None):
    own = (await db.execute(select(BillSettings).where(BillSettings.store_id == store_id))).scalar_one_or_none() if store_id else None
    default = (await db.execute(select(BillSettings).where(BillSettings.store_id.is_(None)))).scalar_one_or_none()
    return own, default


@router.get("")
async def read_bill_settings(
    store_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_any_permission("pos.sale.create", "masterdata.store.view")),
) -> dict:
    """What to print on this store's bills: the store's own details if it has any, else the company default."""
    if store_id:
        require_store_access(store_id, current)
    own, default = await _rows(db, store_id)
    if own is not None:
        return _out(own, "store")
    if default is not None:
        return _out(default, "company")
    return _out(None, "none")


@router.put("")
async def save_bill_settings(
    payload: BillSettingsIn,
    store_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("masterdata.store.update")),
) -> dict:
    """Save for one store, or (no store_id) as the company-wide default."""
    if store_id:
        require_store_access(store_id, current)
    own, default = await _rows(db, store_id)
    row = own if store_id else default
    before = _out(row, "")
    if row is None:
        row = BillSettings(store_id=store_id)
        db.add(row)
    for f in FIELDS:
        value = getattr(payload, f)
        setattr(row, f, (value.strip() if isinstance(value, str) else value) or None)
    await db.flush()
    await write_audit(
        db, user_id=current.user_id, role_code=current.role_code, store_id=store_id, device_id=current.device_id,
        action="UPDATE", entity_type="bill_settings", entity_id=store_id or row.id, old_value=before, new_value=_out(row, ""),
    )
    await db.commit()
    return _out(row, "store" if store_id else "company")
