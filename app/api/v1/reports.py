import io
import uuid

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from openpyxl import Workbook
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission
from app.core.database import get_db
from app.models.models import AuditLog, InventoryBalance, LoyaltyLedger, Product, Sale
from app.services.audit import write_audit

router = APIRouter(prefix="/reports", tags=["reports"])


def _workbook_response(wb: Workbook, filename: str) -> StreamingResponse:
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/sales-register")
async def sales_register(
    store_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("report.export")),
) -> StreamingResponse:
    """Always scoped to the caller's stores — an Admin exporting 'all sales' gets
    their stores, not everyone's — and always audit-logged, since a full sales
    export leaving the business is a real event."""
    stmt = select(Sale)
    if current.role_code != "super_admin":
        stmt = stmt.where(Sale.store_id.in_(current.store_ids))
    if store_id:
        stmt = stmt.where(Sale.store_id == store_id)
    sales = list((await db.execute(stmt.order_by(Sale.billed_at))).scalars().all())

    wb = Workbook()
    ws = wb.active
    ws.title = "Sales"
    ws.append(["Bill Number", "Store", "Cashier", "Billed At", "Subtotal", "Discount", "Tax", "Grand Total", "Status"])
    for s in sales:
        ws.append(
            [s.bill_number, str(s.store_id), str(s.cashier_id), s.billed_at.isoformat(), float(s.subtotal), float(s.discount_total), float(s.tax_total), float(s.grand_total), s.status]
        )

    items_ws = wb.create_sheet("Line Items")
    items_ws.append(["Bill Number", "Product", "Qty", "Unit Price", "Line Discount", "Line Total"])
    for s in sales:
        for item in s.items:
            items_ws.append([s.bill_number, item.product_name_snapshot, float(item.quantity), float(item.unit_price), float(item.line_discount), float(item.line_total)])

    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=store_id,
        device_id=current.device_id,
        action="report.exported",
        entity_type="report",
        entity_id=None,
        new_value={"report": "sales_register", "row_count": len(sales)},
    )
    await db.commit()
    return _workbook_response(wb, "sales_register.xlsx")


@router.get("/inventory-snapshot")
async def inventory_snapshot(
    store_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("report.export")),
) -> StreamingResponse:
    stmt = select(InventoryBalance, Product).join(Product, Product.id == InventoryBalance.product_id)
    if current.role_code != "super_admin":
        stmt = stmt.where(InventoryBalance.store_id.in_(current.store_ids))
    if store_id:
        stmt = stmt.where(InventoryBalance.store_id == store_id)
    rows = (await db.execute(stmt)).all()

    wb = Workbook()
    ws = wb.active
    ws.title = "Inventory"
    ws.append(["SKU", "Product", "Store", "Quantity", "Stock Value"])
    for balance, product in rows:
        ws.append([product.sku, product.name, str(balance.store_id), float(balance.quantity), float(balance.quantity) * float(product.purchase_price)])

    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=store_id,
        device_id=current.device_id,
        action="report.exported",
        entity_type="report",
        entity_id=None,
        new_value={"report": "inventory_snapshot", "row_count": len(rows)},
    )
    await db.commit()
    return _workbook_response(wb, "inventory_snapshot.xlsx")


@router.get("/loyalty")
async def loyalty_report(
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("report.export")),
) -> StreamingResponse:
    rows = list((await db.execute(select(LoyaltyLedger))).scalars().all())
    wb = Workbook()
    ws = wb.active
    ws.title = "Loyalty"
    ws.append(["Customer ID", "Delta Points", "Reason", "Source Type", "Created At"])
    for r in rows:
        ws.append([str(r.customer_id), float(r.delta_points), r.reason, r.source_type, r.created_at.isoformat()])

    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=None,
        device_id=current.device_id,
        action="report.exported",
        entity_type="report",
        entity_id=None,
        new_value={"report": "loyalty"},
    )
    await db.commit()
    return _workbook_response(wb, "loyalty.xlsx")


@router.get("/approvals-audit")
async def approvals_audit_report(
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("report.export")),
) -> StreamingResponse:
    rows = list((await db.execute(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(5000))).scalars().all())
    wb = Workbook()
    ws = wb.active
    ws.title = "Audit Log"
    ws.append(["Timestamp", "User", "Role", "Action", "Entity Type", "Entity ID", "Old Value", "New Value"])
    for r in rows:
        ws.append([r.created_at.isoformat(), str(r.user_id), r.role_code, r.action, r.entity_type, str(r.entity_id), str(r.old_value), str(r.new_value)])
    return _workbook_response(wb, "audit_log.xlsx")
