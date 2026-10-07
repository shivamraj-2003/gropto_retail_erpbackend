"""Resolves record IDs to human-readable labels, so screens never have to
print a raw UUID. One batched call returns labels for every ID a screen is
about to show.

Auth only at the route; each kind checks the permissions that already let the
caller see that kind of record elsewhere — anything they're not allowed to
see simply comes back unresolved and the UI falls back to a short ID.
"""

import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, get_current_user
from app.core.database import get_db
from app.models.models import Customer, Product, Sale, Store, User, Vendor
from app.models.models_phase2 import Grn, Warehouse
from app.models.models_phase3 import Employee
from app.models.models_phase4 import PromotionRule

router = APIRouter(prefix="/lookups", tags=["lookups"])

MAX_IDS_PER_KIND = 200

# kind -> permissions, any one of which lets the caller resolve that kind.
# None = any signed-in user.
KIND_PERMISSIONS: dict[str, tuple[str, ...] | None] = {
    "store": None,
    "warehouse": None,
    "product": ("catalog.product.view", "pos.sale.create", "inventory.stock.view", "wms.pick.view", "procurement.purchase_order.view"),
    "vendor": ("vendor.vendor.view", "procurement.purchase_order.view", "finance.payable.view", "procurement.vendor_invoice.view"),
    "user": (
        "user.user.view", "audit.log.view", "rbac.audit.view", "approval.request.view", "fraud.alert.view",
        "hr.employee.view", "wms.pick.view", "oms.order.view", "alerts.ceo_alert.view", "fraud.suspicious_billing.view",
    ),
    "customer": ("crm.customer.view", "pos.customer.view", "oms.order.view"),
    "sale": ("pos.return.view", "gst.einvoice.view", "finance.expense.view", "reports.sales.export", "pos.sale.create"),
    "grn": ("receiving.grn.view",),
    "employee": ("hr.employee.view", "hr.transfer.view", "workforce.shift.view"),
    "promotion_rule": ("promotion.rule.view", "promotion.analytics.view"),
}


class NamesIn(BaseModel):
    ids: dict[str, list[uuid.UUID]] = Field(default_factory=dict)


def _allowed(current: CurrentUser, kind: str) -> bool:
    perms = KIND_PERMISSIONS.get(kind, ())
    if perms is None:
        return True
    return any(current.has_permission(p) for p in perms)


@router.post("/names")
async def resolve_names(
    payload: NamesIn,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for kind, raw_ids in payload.ids.items():
        ids = list(dict.fromkeys(raw_ids))[:MAX_IDS_PER_KIND]
        if not ids or not _allowed(current, kind):
            continue
        labels: dict[str, str] = {}
        if kind == "store":
            for sid, name, code in (await db.execute(select(Store.id, Store.name, Store.code).where(Store.id.in_(ids)))).all():
                labels[str(sid)] = f"{name} ({code})"
        elif kind == "warehouse":
            for wid, name, code in (await db.execute(select(Warehouse.id, Warehouse.name, Warehouse.code).where(Warehouse.id.in_(ids)))).all():
                labels[str(wid)] = f"{name} ({code})"
        elif kind == "product":
            for pid, name, sku in (await db.execute(select(Product.id, Product.name, Product.sku).where(Product.id.in_(ids)))).all():
                labels[str(pid)] = name
        elif kind == "vendor":
            for vid, name in (await db.execute(select(Vendor.id, Vendor.name).where(Vendor.id.in_(ids)))).all():
                labels[str(vid)] = name
        elif kind == "user":
            for uid, name in (await db.execute(select(User.id, User.full_name).where(User.id.in_(ids)))).all():
                labels[str(uid)] = name
        elif kind == "customer":
            for cid, name, phone in (await db.execute(select(Customer.id, Customer.name, Customer.phone).where(Customer.id.in_(ids)))).all():
                labels[str(cid)] = name or phone or "Customer"
        elif kind == "sale":
            rows = (await db.execute(select(Sale.id, Sale.bill_number, Sale.store_id).where(Sale.id.in_(ids)))).all()
            for sid, bill, store_id in rows:
                if current.owns_store(store_id):
                    labels[str(sid)] = bill
        elif kind == "employee":
            for eid, name, designation in (await db.execute(select(Employee.id, Employee.name, Employee.designation).where(Employee.id.in_(ids)))).all():
                labels[str(eid)] = f"{name} ({designation})" if name else designation
        elif kind == "grn":
            for gid, number in (await db.execute(select(Grn.id, Grn.grn_number).where(Grn.id.in_(ids)))).all():
                if number:
                    labels[str(gid)] = number
        elif kind == "promotion_rule":
            for rid, name in (await db.execute(select(PromotionRule.id, PromotionRule.name).where(PromotionRule.id.in_(ids)))).all():
                labels[str(rid)] = name
        if labels:
            out[kind] = labels
    return out
