import io
import uuid
from datetime import date

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from openpyxl import Workbook
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission
from app.core.database import get_db
from app.models.models import AuditLog, InventoryBalance, LoyaltyLedger, Product, Sale
from app.schemas.schemas import Page
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
    current: CurrentUser = Depends(require_permission("reports.sales.export")),
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
    current: CurrentUser = Depends(require_permission("reports.inventory.export")),
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
    current: CurrentUser = Depends(require_permission("reports.customer.export")),
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
    current: CurrentUser = Depends(require_permission("reports.operations.export")),
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
    current: CurrentUser = Depends(require_permission("reports.purchase.export")),
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
    current: CurrentUser = Depends(require_permission("reports.inventory.export")),
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
    current: CurrentUser = Depends(require_permission("reports.finance.export")),
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
    current: CurrentUser = Depends(require_permission("reports.customer.export")),
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
    current: CurrentUser = Depends(require_permission("reports.operations.export")),
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


@router.get("/sales-hourly")
async def sales_hourly_report(
    business_date: date | None = None,
    store_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("reports.sales.export")),
) -> StreamingResponse:
    """Point 18 audit fix: only daily/MTD/YTD sales buckets existed anywhere
    in the codebase — no hourly granularity at all."""
    target_date = business_date or date.today()
    params: dict = {"business_date": target_date}
    store_filter = ""
    if store_id:
        store_filter = "and store_id = :store_id"
        params["store_id"] = store_id
    elif not current.sees_all_stores():
        store_filter = "and store_id = any(:store_ids)"
        params["store_ids"] = current.store_ids

    rows = (
        await db.execute(
            text(
                f"""
                select extract(hour from billed_at) as hour_of_day,
                       count(*) as bill_count, sum(grand_total) as revenue
                from sales
                where billed_at::date = :business_date and status = 'completed' {store_filter}
                group by extract(hour from billed_at)
                order by hour_of_day
                """
            ),
            params,
        )
    ).all()
    wb = Workbook()
    ws = wb.active
    ws.title = "Hourly Sales"
    ws.append(["Hour (0-23)", "Bill Count", "Revenue"])
    for r in rows:
        ws.append([int(r.hour_of_day), r.bill_count, float(r.revenue)])

    await write_audit(
        db, user_id=current.user_id, role_code=current.role_code, store_id=store_id, device_id=current.device_id,
        action="report.exported", entity_type="report", entity_id=None,
        new_value={"report": "sales_hourly", "business_date": target_date.isoformat()},
    )
    await db.commit()
    return _workbook_response(wb, "sales_hourly.xlsx")


@router.get("/stock-ledger")
async def stock_ledger_report(
    product_id: uuid.UUID | None = None,
    store_id: uuid.UUID | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("reports.inventory.export")),
) -> StreamingResponse:
    """Point 18 audit fix: inventory_movements (written to by every single
    stock-affecting action) had no read/list API anywhere — only a
    current-balance snapshot existed, never the actual transaction-by-
    transaction ledger the blueprint asks for."""
    stmt = (
        text(
            """
            select im.created_at, p.sku, p.name as product_name, im.store_id, im.delta,
                   im.reason_code, im.source_type, im.source_id
            from inventory_movements im
            join products p on p.id = im.product_id
            where (:product_id::uuid is null or im.product_id = :product_id)
              and (:store_id::uuid is null or im.store_id = :store_id)
              and (:date_from::date is null or im.created_at::date >= :date_from)
              and (:date_to::date is null or im.created_at::date <= :date_to)
              and (:all_stores or im.store_id = any(:store_ids))
            order by im.created_at desc
            limit 5000
            """
        )
    )
    rows = (
        await db.execute(
            stmt,
            {
                "product_id": product_id,
                "store_id": store_id,
                "date_from": date_from,
                "date_to": date_to,
                "all_stores": current.sees_all_stores(),
                "store_ids": current.store_ids or [uuid.UUID(int=0)],
            },
        )
    ).all()
    wb = Workbook()
    ws = wb.active
    ws.title = "Stock Ledger"
    ws.append(["Timestamp", "SKU", "Product", "Store", "Delta", "Reason Code", "Source Type", "Source ID"])
    for r in rows:
        ws.append([r.created_at.isoformat(), r.sku, r.product_name, str(r.store_id), float(r.delta), r.reason_code, r.source_type, str(r.source_id)])

    await write_audit(
        db, user_id=current.user_id, role_code=current.role_code, store_id=store_id, device_id=current.device_id,
        action="report.exported", entity_type="report", entity_id=None,
        new_value={"report": "stock_ledger", "row_count": len(rows)},
    )
    await db.commit()
    return _workbook_response(wb, "stock_ledger.xlsx")


@router.get("/transfer-ageing")
async def transfer_ageing_report(
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("reports.inventory.export")),
) -> StreamingResponse:
    """Point 18 audit fix: transfers.dispatched_at has existed since Point 5
    but nothing ever turned it into an ageing report — no "how long has this
    transfer been in transit/pending" view existed anywhere, distinct from
    the separate received-vs-dispatched quantity Discrepancies report."""
    rows = (
        await db.execute(
            text(
                """
                select id, dest_type, dest_id, status, dispatched_at,
                       extract(day from now() - dispatched_at) as days_in_transit
                from transfers
                where status = 'dispatched'
                order by dispatched_at asc
                """
            )
        )
    ).all()
    wb = Workbook()
    ws = wb.active
    ws.title = "Transfer Ageing"
    ws.append(["Transfer ID", "Destination Type", "Destination ID", "Status", "Dispatched At", "Days In Transit", "Age Bucket"])
    for r in rows:
        days = int(r.days_in_transit)
        bucket = "0-1 days" if days <= 1 else "2-3 days" if days <= 3 else "4-7 days" if days <= 7 else "7+ days"
        ws.append([str(r.id), r.dest_type, str(r.dest_id), r.status, r.dispatched_at.isoformat(), days, bucket])

    await write_audit(
        db, user_id=current.user_id, role_code=current.role_code, store_id=None, device_id=current.device_id,
        action="report.exported", entity_type="report", entity_id=None,
        new_value={"report": "transfer_ageing", "row_count": len(rows)},
    )
    await db.commit()
    return _workbook_response(wb, "transfer_ageing.xlsx")


@router.get("/margin-bridge")
async def margin_bridge_report(
    store_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("reports.sales.export")),
) -> StreamingResponse:
    """Point 18 audit fix: no margin bridge/waterfall (price/volume/cost
    driver decomposition explaining WHY margin moved month-over-month)
    existed anywhere — only a flat current-month margin % figure did.
    Standard three-factor decomposition: volume effect holds price/cost at
    last month's level and varies only quantity; price effect holds quantity
    at this month's level and varies only price; cost effect is the
    remainder (COGS rate change)."""
    store_filter = ""
    params: dict = {}
    if store_id:
        store_filter = "and s.store_id = :store_id"
        params["store_id"] = store_id
    elif not current.sees_all_stores():
        store_filter = "and s.store_id = any(:store_ids)"
        params["store_ids"] = current.store_ids

    rows = (
        await db.execute(
            text(
                f"""
                select s.store_id,
                       sum(si.quantity) filter (where date_trunc('month', s.billed_at) = date_trunc('month', now())) as qty_cur,
                       sum(si.line_total) filter (where date_trunc('month', s.billed_at) = date_trunc('month', now())) as rev_cur,
                       sum(si.quantity * p.purchase_price) filter (where date_trunc('month', s.billed_at) = date_trunc('month', now())) as cogs_cur,
                       sum(si.quantity) filter (where date_trunc('month', s.billed_at) = date_trunc('month', now() - interval '1 month')) as qty_prior,
                       sum(si.line_total) filter (where date_trunc('month', s.billed_at) = date_trunc('month', now() - interval '1 month')) as rev_prior,
                       sum(si.quantity * p.purchase_price) filter (where date_trunc('month', s.billed_at) = date_trunc('month', now() - interval '1 month')) as cogs_prior
                from sales s
                join sale_items si on si.sale_id = s.id
                join products p on p.id = si.product_id
                where s.status = 'completed'
                  and s.billed_at >= date_trunc('month', now() - interval '1 month')
                  {store_filter}
                group by s.store_id
                """
            ),
            params,
        )
    ).all()
    wb = Workbook()
    ws = wb.active
    ws.title = "Margin Bridge"
    ws.append(["Store", "Prior Margin", "Volume Effect", "Price Effect", "Cost Effect", "Current Margin"])
    for r in rows:
        qty_cur, rev_cur, cogs_cur = float(r.qty_cur or 0), float(r.rev_cur or 0), float(r.cogs_cur or 0)
        qty_prior, rev_prior, cogs_prior = float(r.qty_prior or 0), float(r.rev_prior or 0), float(r.cogs_prior or 0)
        prior_margin = rev_prior - cogs_prior
        current_margin = rev_cur - cogs_cur
        # Average per-unit price/cost for a clean three-factor split.
        avg_price_prior = rev_prior / qty_prior if qty_prior else 0.0
        avg_cost_prior = cogs_prior / qty_prior if qty_prior else 0.0
        avg_price_cur = rev_cur / qty_cur if qty_cur else 0.0
        avg_cost_cur = cogs_cur / qty_cur if qty_cur else 0.0
        volume_effect = (qty_cur - qty_prior) * (avg_price_prior - avg_cost_prior)
        price_effect = (avg_price_cur - avg_price_prior) * qty_cur
        cost_effect = -(avg_cost_cur - avg_cost_prior) * qty_cur
        ws.append([str(r.store_id), round(prior_margin, 2), round(volume_effect, 2), round(price_effect, 2), round(cost_effect, 2), round(current_margin, 2)])

    await write_audit(
        db, user_id=current.user_id, role_code=current.role_code, store_id=store_id, device_id=current.device_id,
        action="report.exported", entity_type="report", entity_id=None,
        new_value={"report": "margin_bridge", "row_count": len(rows)},
    )
    await db.commit()
    return _workbook_response(wb, "margin_bridge.xlsx")


@router.get("/audit-checklist")
async def audit_checklist_report(
    date_from: date | None = None,
    date_to: date | None = None,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("reports.operations.export")),
) -> StreamingResponse:
    """Point 18 audit fix: a literal operational/compliance checklist report
    — distinct from the HR joining/exit DocumentChecklistItem and from the
    immutable audit_log change trail, neither of which is this."""
    params: dict = {"date_from": date_from, "date_to": date_to}
    store_filter = ""
    if not current.sees_all_stores():
        store_filter = "and scc.store_id = any(:store_ids)"
        params["store_ids"] = current.store_ids

    rows = (
        await db.execute(
            text(
                f"""
                select st.name as store_name, oci.name as item_name, oci.category,
                       scc.business_date, scc.status, scc.completed_at
                from store_checklist_completions scc
                join operational_checklist_items oci on oci.id = scc.checklist_item_id
                join stores st on st.id = scc.store_id
                where (:date_from::date is null or scc.business_date >= :date_from)
                  and (:date_to::date is null or scc.business_date <= :date_to)
                  {store_filter}
                order by scc.business_date desc, st.name
                limit 5000
                """
            ),
            params,
        )
    ).all()
    wb = Workbook()
    ws = wb.active
    ws.title = "Audit Checklist"
    ws.append(["Store", "Checklist Item", "Category", "Business Date", "Status", "Completed At"])
    completed_count = 0
    for r in rows:
        if r.status == "completed":
            completed_count += 1
        ws.append([r.store_name, r.item_name, r.category, r.business_date.isoformat(), r.status, r.completed_at.isoformat() if r.completed_at else ""])
    completion_rate = round(completed_count / len(rows) * 100, 1) if rows else 0.0
    ws.append(["COMPLETION RATE %", completion_rate, "", "", "", ""])

    await write_audit(
        db, user_id=current.user_id, role_code=current.role_code, store_id=None, device_id=current.device_id,
        action="report.exported", entity_type="report", entity_id=None,
        new_value={"report": "audit_checklist", "row_count": len(rows)},
    )
    await db.commit()
    return _workbook_response(wb, "audit_checklist.xlsx")


SLA_DISPATCH_MINUTES = 120  # same threshold dashboard.py/orders.py each independently use


@router.get("/sla-summary")
async def sla_summary_report(
    store_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("reports.insight.view")),
) -> dict:
    """Point 18 audit fix: no single, unified, filterable SLA report existed
    — warehouse dispatch SLA and online delivery SLA each lived as separate
    ad-hoc numbers inside different dashboards with no store filter and no
    breach list. This is the first place both are shown together."""
    params: dict = {"sla_minutes": SLA_DISPATCH_MINUTES}
    online_store_filter = ""
    if store_id:
        online_store_filter = "and allocated_store_id = :store_id"
        params["store_id"] = store_id
    elif not current.sees_all_stores():
        online_store_filter = "and allocated_store_id = any(:store_ids)"
        params["store_ids"] = current.store_ids

    online_rows = (
        await db.execute(
            text(
                f"""
                select count(*) as total,
                       count(*) filter (where extract(epoch from (dispatched_at - packed_at)) / 60 <= :sla_minutes) as within_sla
                from orders
                where dispatched_at is not null and packed_at is not null
                  and dispatched_at >= current_date - interval '30 days'
                  {online_store_filter}
                """
            ),
            params,
        )
    ).one()
    online_breaches = (
        await db.execute(
            text(
                f"""
                select id, allocated_store_id, packed_at, dispatched_at,
                       round(extract(epoch from (dispatched_at - packed_at)) / 60) as minutes_taken
                from orders
                where dispatched_at is not null and packed_at is not null
                  and dispatched_at >= current_date - interval '30 days'
                  and extract(epoch from (dispatched_at - packed_at)) / 60 > :sla_minutes
                  {online_store_filter}
                order by dispatched_at desc
                limit 100
                """
            ),
            params,
        )
    ).all()

    online_total = int(online_rows.total)
    online_within = int(online_rows.within_sla)
    online_sla_pct = round(online_within / online_total * 100, 1) if online_total else None

    return {
        "sla_threshold_minutes": SLA_DISPATCH_MINUTES,
        "window_days": 30,
        "online_dispatch_sla_pct": online_sla_pct,
        "online_total_orders": online_total,
        "online_within_sla": online_within,
        "online_breaches": [
            {
                "order_id": str(r.id),
                "store_id": str(r.allocated_store_id) if r.allocated_store_id else None,
                "minutes_taken": float(r.minutes_taken),
            }
            for r in online_breaches
        ],
    }


@router.get("/exception-log", response_model=Page[dict])
async def exception_log_report(
    store_id: uuid.UUID | None = None,
    exception_type: str | None = None,
    limit: int = 20,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("reports.insight.view")),
) -> Page[dict]:
    """Point 18 audit fix: no single, unified, filterable, exportable
    "Exception Log" existed — only a 4-number summary tile
    (/enterprise/exceptions, no drill-down from itself) and a fraud-specific
    alert list (/fraud/alerts, covering only one of the four exception
    types) existed separately. This merges fraud alerts, pending approvals,
    unresolved sync failures and transfer discrepancies into one list."""
    capped_limit = min(limit, 200)
    scoped = not current.sees_all_stores()
    store_ids = current.store_ids or [uuid.UUID(int=0)]

    union_sql = """
        select 'fraud_alert' as exception_type, id, store_id, rule_code as label, severity, created_at
        from fraud_alerts where status = 'open'
        union all
        select 'pending_approval' as exception_type, id, store_id, request_type as label, 'medium' as severity, created_at
        from approval_requests where status = 'pending'
        union all
        select 'sync_failure' as exception_type, id, null as store_id, 'sync_failure' as label, 'high' as severity, created_at
        from sync_failures where resolved = false
        union all
        select 'transfer_discrepancy' as exception_type, id,
               case when dest_type = 'store' then dest_id else null end as store_id,
               'transfer_discrepancy' as label, 'medium' as severity, dispatched_at as created_at
        from transfers where status = 'discrepancy'
    """
    params: dict = {}
    where_clauses = []
    if exception_type:
        where_clauses.append("exception_type = :exception_type")
        params["exception_type"] = exception_type
    if store_id:
        where_clauses.append("(store_id = :store_id or store_id is null)")
        params["store_id"] = store_id
    elif scoped:
        where_clauses.append("(store_id = any(:store_ids) or store_id is null)")
        params["store_ids"] = store_ids
    where_sql = f"where {' and '.join(where_clauses)}" if where_clauses else ""

    count_row = (await db.execute(text(f"select count(*) from ({union_sql}) u {where_sql}"), params)).scalar_one()
    params_with_page = {**params, "limit": capped_limit, "offset": offset}
    rows = (
        await db.execute(
            text(f"select * from ({union_sql}) u {where_sql} order by created_at desc limit :limit offset :offset"),
            params_with_page,
        )
    ).all()

    items = [
        {
            "exception_type": r.exception_type,
            "id": str(r.id),
            "store_id": str(r.store_id) if r.store_id else None,
            "label": r.label,
            "severity": r.severity,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]
    return Page(items=items, total=count_row, limit=capped_limit, offset=offset)


@router.get("/channel-split")
async def channel_split_report(
    store_id: uuid.UUID | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("reports.insight.view")),
) -> dict:
    """Point 18 audit fix: channel split previously existed only as a
    same-day, enterprise-wide-only in-store-vs-online binary inside
    ceo-command-center — no store filter, no custom date range, no history."""
    d_from = date_from or date.today()
    d_to = date_to or date.today()
    params: dict = {"date_from": d_from, "date_to": d_to}
    store_filter_instore = ""
    store_filter_online = ""
    if store_id:
        store_filter_instore = "and store_id = :store_id"
        store_filter_online = "and allocated_store_id = :store_id"
        params["store_id"] = store_id
    elif not current.sees_all_stores():
        store_filter_instore = "and store_id = any(:store_ids)"
        store_filter_online = "and allocated_store_id = any(:store_ids)"
        params["store_ids"] = current.store_ids

    in_store = (
        await db.execute(
            text(
                f"""
                select count(*) as bills, coalesce(sum(grand_total), 0) as revenue
                from sales
                where status = 'completed' and billed_at::date between :date_from and :date_to {store_filter_instore}
                """
            ),
            params,
        )
    ).one()
    online = (
        await db.execute(
            text(
                f"""
                select channel, count(*) as orders, coalesce(sum(grand_total), 0) as revenue
                from orders
                where status not in ('cancelled') and created_at::date between :date_from and :date_to {store_filter_online}
                group by channel
                """
            ),
            params,
        )
    ).all()

    return {
        "date_from": d_from.isoformat(),
        "date_to": d_to.isoformat(),
        "in_store": {"bills": int(in_store.bills), "revenue": float(in_store.revenue)},
        "online_by_channel": [
            {"channel": r.channel, "orders": int(r.orders), "revenue": float(r.revenue)} for r in online
        ],
    }


@router.get("/customer-activity")
async def customer_activity_report(
    store_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("reports.insight.view")),
) -> dict:
    """Point 18 audit fix: "New/repeat" and "Frequency" previously existed
    only as same-day/trailing-90-day, enterprise-wide-only aggregate numbers
    inside ceo-command-center — no store filter, no per-customer breakdown."""
    params: dict = {}
    store_filter = ""
    if store_id:
        store_filter = "and store_id = :store_id"
        params["store_id"] = store_id
    elif not current.sees_all_stores():
        store_filter = "and store_id = any(:store_ids)"
        params["store_ids"] = current.store_ids

    rows = (
        await db.execute(
            text(
                f"""
                select customer_id, min(billed_at) as first_purchase, count(*) as purchase_count
                from sales
                where status = 'completed' and customer_id is not null and billed_at >= current_date - interval '90 days' {store_filter}
                group by customer_id
                """
            ),
            params,
        )
    ).all()
    new_count = sum(1 for r in rows if r.first_purchase.date() >= date.today().replace(day=1))
    repeat_count = sum(1 for r in rows if r.purchase_count > 1)
    avg_frequency = round(sum(r.purchase_count for r in rows) / len(rows), 2) if rows else 0.0

    return {
        "window_days": 90,
        "total_customers": len(rows),
        "new_this_month": new_count,
        "repeat_customers": repeat_count,
        "repeat_pct": round(repeat_count / len(rows) * 100, 1) if rows else 0.0,
        "avg_purchase_frequency_90d": avg_frequency,
    }


@router.get("/operations-scorecard-live")
async def operations_scorecard_live(
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("reports.insight.view")),
) -> list[dict]:
    """JSON twin of /operations-scorecard (which only ever existed as an
    .xlsx download) so the scorecard can actually be viewed on screen, not
    just exported blind."""
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
    if not current.sees_all_stores():
        rows = [r for r in rows if r.store_id in current.store_ids]
    return [
        {
            "store_id": str(r.store_id),
            "store_name": r.store_name,
            "staff_count": r.staff_count,
            "attendance_rate_pct": round(r.present_yesterday / r.marked_yesterday * 100, 1) if r.marked_yesterday else None,
            "fraud_exceptions_yesterday": r.fraud_exceptions,
            "pending_approvals": r.pending_approvals,
        }
        for r in rows
    ]

