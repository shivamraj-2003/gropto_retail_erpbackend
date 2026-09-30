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
from app.services import crm as crm_service
from app.services import finance as finance_service
from app.services.audit import write_audit
from sqlalchemy import text

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
    if not current.sees_all_stores():
        stmt = stmt.where(Sale.store_id.in_(current.store_ids))
    if store_id:
        stmt = stmt.where(Sale.store_id == store_id)
    sales = list((await db.execute(stmt.order_by(Sale.billed_at))).scalars().all())

    wb = Workbook()
    ws = wb.active
    ws.title = "Sales"
    ws.append(["Bill Number", "Store", "Cashier", "Billed At", "Taxable Value", "Discount", "GST (CGST+SGST)", "Grand Total", "Status"])
    tot_subtotal = tot_discount = tot_tax = tot_grand = 0.0
    for s in sales:
        ws.append(
            [s.bill_number, str(s.store_id), str(s.cashier_id), s.billed_at.isoformat(), float(s.subtotal), float(s.discount_total), float(s.tax_total), float(s.grand_total), s.status]
        )
        tot_subtotal += float(s.subtotal)
        tot_discount += float(s.discount_total)
        tot_tax += float(s.tax_total)
        tot_grand += float(s.grand_total)
    ws.append(["TOTAL", "", "", "", tot_subtotal, tot_discount, tot_tax, tot_grand, ""])

    items_ws = wb.create_sheet("Line Items")
    items_ws.append(
        ["Bill Number", "Product", "HSN/SAC", "Qty", "Unit Price (incl. GST)", "Line Discount", "Line Total",
         "Taxable Value", "GST Rate %", "CGST", "SGST"]
    )
    tot_qty = tot_line_disc = tot_line_tot = tot_taxable = tot_cgst = tot_sgst = 0.0
    for s in sales:
        for item in s.items:
            items_ws.append(
                [s.bill_number, item.product_name_snapshot, item.hsn_code_snapshot or "", float(item.quantity),
                 float(item.unit_price), float(item.line_discount), float(item.line_total),
                 float(item.taxable_value), float(item.tax_rate_snapshot), float(item.cgst_amount), float(item.sgst_amount)]
            )
            tot_qty += float(item.quantity)
            tot_line_disc += float(item.line_discount)
            tot_line_tot += float(item.line_total)
            tot_taxable += float(item.taxable_value)
            tot_cgst += float(item.cgst_amount)
            tot_sgst += float(item.sgst_amount)
    items_ws.append(["TOTAL", "", "", tot_qty, "", tot_line_disc, tot_line_tot, tot_taxable, "", tot_cgst, tot_sgst])

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
    if not current.sees_all_stores():
        stmt = stmt.where(InventoryBalance.store_id.in_(current.store_ids))
    if store_id:
        stmt = stmt.where(InventoryBalance.store_id == store_id)
    rows = (await db.execute(stmt)).all()

    wb = Workbook()
    ws = wb.active
    ws.title = "Inventory"
    ws.append(["SKU", "Product", "Store", "Quantity", "Stock Value"])
    tot_qty = tot_val = 0.0
    for balance, product in rows:
        val = float(balance.quantity) * float(product.purchase_price)
        ws.append([product.sku, product.name, str(balance.store_id), float(balance.quantity), val])
        tot_qty += float(balance.quantity)
        tot_val += val
    ws.append(["TOTAL", "", "", tot_qty, tot_val])

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
    tot_pts = 0.0
    for r in rows:
        ws.append([str(r.customer_id), float(r.delta_points), r.reason, r.source_type, r.created_at.isoformat()])
        tot_pts += float(r.delta_points)
    ws.append(["TOTAL", tot_pts, "", "", ""])

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


@router.get("/purchase-trend")
async def purchase_trend_report(
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("report.export")),
) -> StreamingResponse:
    rows = (
        await db.execute(
            text(
                """
                select po.id, v.name as vendor_name, po.total_amount, po.status, po.created_at,
                       rate_variance.avg_variance_pct
                from purchase_orders po
                join vendors v on v.id = po.vendor_id
                left join lateral (
                    select avg(
                        case when poi.unit_cost > 0
                            then (poi.unit_cost - coalesce(prior.unit_cost, poi.unit_cost)) / poi.unit_cost * 100
                            else 0 end
                    ) as avg_variance_pct
                    from purchase_order_items poi
                    left join lateral (
                        select unit_cost from purchase_order_items poi2
                        join purchase_orders po2 on po2.id = poi2.purchase_order_id
                        where poi2.product_id = poi.product_id and po2.created_at < po.created_at
                        order by po2.created_at desc limit 1
                    ) prior on true
                    where poi.purchase_order_id = po.id
                ) rate_variance on true
                order by po.created_at desc
                limit 5000
                """
            )
        )
    ).all()
    wb = Workbook()
    ws = wb.active
    ws.title = "Purchase Trend"
    ws.append(["PO ID", "Vendor", "Total Amount", "Status", "Created At", "Avg Rate Variance %"])
    for r in rows:
        ws.append([
            str(r.id), r.vendor_name, float(r.total_amount), r.status, r.created_at.isoformat(),
            round(float(r.avg_variance_pct), 2) if r.avg_variance_pct is not None else None,
        ])
    return _workbook_response(wb, "purchase_trend.xlsx")


@router.get("/warehouse-discrepancies")
async def warehouse_report(
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("report.export")),
) -> StreamingResponse:
    rows = (
        await db.execute(
            text(
                """
                select g.id as grn_id, g.warehouse_id, g.status, g.created_at,
                       count(gi.id) filter (where gi.received_qty <> gi.expected_qty) as line_variances,
                       count(gi.id) filter (where gi.qc_status <> 'accepted') as qc_rejections,
                       sum(gi.expected_qty - gi.received_qty) as total_qty_variance
                from grn g
                left join grn_items gi on gi.grn_id = g.id
                group by g.id, g.warehouse_id, g.status, g.created_at
                order by g.created_at desc
                limit 5000
                """
            )
        )
    ).all()
    wb = Workbook()
    ws = wb.active
    ws.title = "Warehouse Discrepancies"
    ws.append(["GRN ID", "Warehouse ID", "Status", "Line Variances", "QC Rejections", "Total Qty Variance", "Created At"])
    for r in rows:
        ws.append([
            str(r.grn_id), str(r.warehouse_id) if r.warehouse_id else None, r.status,
            r.line_variances, r.qc_rejections,
            float(r.total_qty_variance) if r.total_qty_variance is not None else 0,
            r.created_at.isoformat(),
        ])
    return _workbook_response(wb, "warehouse_report.xlsx")


@router.get("/finance-pnl")
async def finance_pnl_report(
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("report.export")),
) -> StreamingResponse:
    store_rows = (await db.execute(text("select id, name from stores where is_active = true"))).all()
    payables = await finance_service.payables_ageing(db)
    payables_by_vendor_total = sum(p["amount_due"] for p in payables)

    wb = Workbook()
    ws = wb.active
    ws.title = "Finance PnL"
    ws.append(["Store", "MTD Revenue", "MTD Discounts", "MTD Expenses", "Net Contribution", "Company Payables Outstanding"])
    for s in store_rows:
        pnl = await finance_service.store_pnl(db, store_id=s.id)
        net = pnl["estimated_contribution_mtd"] - pnl["discounts_mtd"]
        ws.append([s.name, pnl["revenue_mtd"], pnl["discounts_mtd"], pnl["expenses_mtd"], net, None])
    ws.append(["TOTAL (company payables outstanding)", None, None, None, None, payables_by_vendor_total])
    return _workbook_response(wb, "finance_pnl.xlsx")


@router.get("/customer-rfm")
async def customer_rfm_report(
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("report.export")),
) -> StreamingResponse:
    rows = await crm_service.rfm_segments(db)
    wb = Workbook()
    ws = wb.active
    ws.title = "Customer RFM"
    ws.append(["Customer ID", "Segment", "Recency (days)", "Frequency", "Monetary"])
    for r in rows:
        ws.append([r["customer_id"], r["segment"], r["recency_days"], r["frequency"], r["monetary"]])
    return _workbook_response(wb, "customer_rfm.xlsx")


@router.get("/operations-scorecard")
async def operations_scorecard_report(
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("report.export")),
) -> StreamingResponse:
    rows = (
        await db.execute(
            text(
                """
                select s.id as store_id, s.name as store_name,
                    (select count(*) from employees e where e.store_id = s.id and e.is_active = true) as staff_count,
                    (select count(*) from attendance a
                       join employees e2 on e2.id = a.employee_id
                       where e2.store_id = s.id and a.attendance_date = current_date - interval '1 day') as marked_yesterday,
                    (select count(*) from attendance a2
                       join employees e3 on e3.id = a2.employee_id
                       where e3.store_id = s.id and a2.attendance_date = current_date - interval '1 day' and a2.status = 'present') as present_yesterday,
                    (select count(*) from fraud_alerts fa where fa.store_id = s.id and fa.created_at::date = current_date - interval '1 day') as fraud_exceptions,
                    (select count(*) from approval_requests ar where ar.store_id = s.id and ar.status = 'pending') as pending_approvals
                from stores s
                where s.is_active = true
                order by s.name
                """
            )
        )
    ).all()
    wb = Workbook()
    ws = wb.active
    ws.title = "Operations Scorecard"
    ws.append(["Store", "Staff Count", "Attendance Marked (yesterday)", "Present (yesterday)", "Attendance Rate %", "Fraud Exceptions (yesterday)", "Pending Approvals", "Exception Count"])
    for r in rows:
        attendance_rate = round(r.present_yesterday / r.marked_yesterday * 100, 1) if r.marked_yesterday else None
        exception_count = r.fraud_exceptions + r.pending_approvals
        ws.append([
            r.store_name, r.staff_count, r.marked_yesterday, r.present_yesterday,
            attendance_rate, r.fraud_exceptions, r.pending_approvals, exception_count,
        ])
    return _workbook_response(wb, "operations_scorecard.xlsx")

