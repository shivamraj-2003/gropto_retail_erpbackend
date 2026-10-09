"""One simple "Download as Excel" for the lists people work with every day.

GET /exports/{kind} returns an .xlsx of that list. Each kind needs the same
permission as the screen it comes from, and store-scoped users only get rows for
their own stores. Secrets (password hashes, MFA secrets, bank account numbers)
are never included.
"""

import io
import re
import uuid
from dataclasses import dataclass

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from openpyxl import Workbook
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, get_current_user, require_store_access
from app.core.database import get_reporting_db

router = APIRouter(prefix="/exports", tags=["exports"])


@dataclass(frozen=True)
class Kind:
    permission: str
    title: str
    sql: str
    headers: list[str]
    # Row filter for store-scoped users; it replaces {scope} in the query. Empty = the list is not store-specific.
    scope: str = ""


# Every query is a fixed string; the only value ever bound is the caller's own store list.
KINDS: dict[str, Kind] = {
    "stores": Kind(
        "masterdata.store.view", "Stores",
        """select s.code, s.name, s.city, s.state, s.gstin, s.area_sqft, s.target_revenue_monthly,
                  case when s.is_active then 'Active' else 'Switched off' end
           from public.stores s where {scope} order by s.name""",
        ["Code", "Name", "City", "State", "GSTIN", "Area (sq ft)", "Monthly target", "Status"],
        scope="s.id = any(:store_ids)",
    ),
    "users": Kind(
        "user.user.view", "Users",
        """select u.full_name, u.email, u.phone, r.name, coalesce((select string_agg(s.name, ', ' order by s.name)
                    from public.user_stores us join public.stores s on s.id = us.store_id where us.user_id = u.id), ''),
                  case when u.is_active then 'Active' else 'Switched off' end
           from public.users u join public.roles r on r.id = u.role_id where {scope} order by u.full_name""",
        ["Name", "Email", "Phone", "Role", "Stores", "Status"],
        scope="exists (select 1 from public.user_stores us2 where us2.user_id = u.id and us2.store_id = any(:store_ids))",
    ),
    "vendors": Kind(
        "vendor.vendor.view", "Vendors",
        """select v.name, v.category, v.gst_number, v.phone, v.email, v.credit_days,
                  case when v.is_active then 'Active' else 'Switched off' end
           from public.vendors v where {scope} order by v.name""",
        ["Name", "Category", "GSTIN", "Phone", "Email", "Credit days", "Status"],
    ),
    "customers": Kind(
        "crm.customer.view", "Customers",
        """select c.name, c.phone, c.email, to_char(c.created_at, 'YYYY-MM-DD'),
                  coalesce((select sum(delta_points) from public.loyalty_ledger l where l.customer_id = c.id), 0)
           from public.customers c where {scope} order by c.name nulls last, c.phone""",
        ["Name", "Phone", "Email", "Joined", "Loyalty points"],
    ),
    "employees": Kind(
        "hr.employee.view", "Staff",
        """select e.name, e.designation, s.name, to_char(e.joined_at, 'YYYY-MM-DD'),
                  case when e.is_active then 'Active' else 'Left' end
           from public.employees e left join public.stores s on s.id = e.store_id where {scope} order by e.name""",
        ["Name", "Designation", "Store", "Joined", "Status"],
        scope="e.store_id = any(:store_ids)",
    ),
    "purchase-orders": Kind(
        "procurement.purchase_order.view", "Purchase orders",
        """select 'PO-' || upper(left(po.id::text, 8)), to_char(po.created_at, 'YYYY-MM-DD HH24:MI'), v.name, s.name, po.status, po.total_amount
           from public.purchase_orders po
           left join public.vendors v on v.id = po.vendor_id left join public.stores s on s.id = po.store_id
           where {scope} order by po.created_at desc""",
        ["PO number", "Date", "Vendor", "Store", "Status", "Amount"],
        scope="po.store_id = any(:store_ids)",
    ),
    "orders": Kind(
        "oms.order.view", "Online orders",
        """select upper(left(o.id::text, 8)), to_char(o.created_at, 'YYYY-MM-DD HH24:MI'), o.channel, s.name, c.name, c.phone,
                  o.status, o.payment_mode, o.payment_status, o.subtotal, o.discount_total, o.delivery_fee, o.grand_total
           from public.orders o
           left join public.stores s on s.id = o.allocated_store_id left join public.customers c on c.id = o.customer_id
           where {scope} order by o.created_at desc""",
        ["Order", "Date", "Channel", "Store", "Customer", "Phone", "Status", "Payment mode", "Payment status", "Subtotal", "Discount", "Delivery fee", "Total"],
        scope="o.allocated_store_id = any(:store_ids)",
    ),
    "bills": Kind(
        "reports.sales.export", "Bills",
        """select sa.bill_number, to_char(sa.billed_at, 'YYYY-MM-DD HH24:MI'), s.name, u.full_name,
                  (select count(*) from public.sale_items si where si.sale_id = sa.id),
                  coalesce((select string_agg(distinct pm.mode, ', ') from public.payments pm where pm.sale_id = sa.id), ''),
                  sa.discount_total, sa.tax_total, sa.grand_total, sa.status
           from public.sales sa left join public.stores s on s.id = sa.store_id left join public.users u on u.id = sa.cashier_id
           where {scope} order by sa.billed_at desc""",
        ["Bill", "Billed at", "Store", "Cashier", "Items", "Paid by", "Discount", "GST", "Total", "Status"],
        scope="sa.store_id = any(:store_ids)",
    ),
    "returns": Kind(
        "pos.return.view", "Returns",
        """select upper(left(r.id::text, 8)), to_char(r.created_at, 'YYYY-MM-DD HH24:MI'), s.name, r.reason, r.status,
                  case when r.is_exchange then 'Exchange' else 'Refund' end, r.refund_total
           from public.returns r left join public.stores s on s.id = r.store_id where {scope} order by r.created_at desc""",
        ["Return", "Date", "Store", "Reason", "Status", "Type", "Refund amount"],
        scope="r.store_id = any(:store_ids)",
    ),
    "expenses": Kind(
        "finance.expense.view", "Expenses",
        """select to_char(e.created_at, 'YYYY-MM-DD'), s.name, e.category, e.description, e.amount, e.status
           from public.expenses e left join public.stores s on s.id = e.store_id where {scope} order by e.created_at desc""",
        ["Date", "Store", "Category", "Description", "Amount", "Status"],
        scope="e.store_id = any(:store_ids)",
    ),
    "payables": Kind(
        "finance.payable.view", "Payables",
        """select v.name, pb.original_amount, pb.amount_due, to_char(pb.due_date, 'YYYY-MM-DD'), pb.status,
                  coalesce(vi.invoice_number, ''), to_char(pb.created_at, 'YYYY-MM-DD')
           from public.payables pb left join public.vendors v on v.id = pb.vendor_id
           left join public.vendor_invoices vi on vi.id = pb.vendor_invoice_id
           where {scope} order by pb.due_date nulls last, v.name""",
        ["Vendor", "Original amount", "Amount due", "Due date", "Status", "Invoice number", "Created"],
    ),
    "vendor-payments": Kind(
        "finance.payable.view", "Vendor payments",
        """select to_char(vp.created_at, 'YYYY-MM-DD HH24:MI'), v.name, vp.amount, vp.reference, vp.status
           from public.vendor_payments vp join public.payables pb on pb.id = vp.payable_id
           left join public.vendors v on v.id = pb.vendor_id
           where {scope} order by vp.created_at desc""",
        ["Date", "Vendor", "Amount", "Reference", "Status"],
    ),
    "vendor-invoices": Kind(
        "procurement.vendor_invoice.view", "Vendor invoices",
        """select vi.invoice_number, to_char(vi.invoice_date, 'YYYY-MM-DD'), v.name, vi.taxable_value, vi.cgst_amount, vi.sgst_amount,
                  vi.igst_amount, vi.invoice_amount, vi.status
           from public.vendor_invoices vi left join public.vendors v on v.id = vi.vendor_id
           where {scope} order by vi.invoice_date desc""",
        ["Invoice number", "Invoice date", "Vendor", "Taxable value", "CGST", "SGST", "IGST", "Invoice amount", "Status"],
    ),
    "bank-deposits": Kind(
        "finance.bank_deposit.view", "Bank deposits",
        """select to_char(bd.business_date, 'YYYY-MM-DD'), s.name, bd.cash_expected, bd.cash_deposited, bd.variance, bd.bank_name, bd.slip_reference, bd.status
           from public.bank_deposits bd left join public.stores s on s.id = bd.store_id
           where {scope} order by bd.business_date desc""",
        ["Date", "Store", "Cash expected", "Cash deposited", "Difference", "Bank", "Slip reference", "Status"],
        scope="bd.store_id = any(:store_ids)",
    ),
    "budgets": Kind(
        "finance.budget.view", "Budgets",
        """select s.name, b.financial_year, b.month, b.opex_budget, b.actual_opex, b.capex_budget, b.actual_capex
           from public.store_budgets b left join public.stores s on s.id = b.store_id
           where {scope} order by b.financial_year desc, b.month, s.name""",
        ["Store", "Financial year", "Month", "Opex budget", "Opex actual", "Capex budget", "Capex actual"],
        scope="b.store_id = any(:store_ids)",
    ),
    "einvoices": Kind(
        "gst.einvoice.view", "E-invoices",
        """select to_char(e.created_at, 'YYYY-MM-DD HH24:MI'), s.name, sa.bill_number, sa.customer_gstin, sa.grand_total, e.status,
                  coalesce(e.irn, ''), coalesce(e.ack_no, ''), e.retry_count
           from public.einvoices e join public.sales sa on sa.id = e.sale_id left join public.stores s on s.id = sa.store_id
           where {scope} order by e.created_at desc""",
        ["Created", "Store", "Bill number", "Customer GSTIN", "Bill total", "Status", "IRN", "Ack no", "Retries"],
        scope="sa.store_id = any(:store_ids)",
    ),
    "shifts": Kind(
        "pos.shift.view", "Shifts",
        """select to_char(sh.opened_at, 'YYYY-MM-DD HH24:MI'), s.name, u.full_name, sh.opening_float, sh.expected_cash, sh.counted_cash,
                  sh.variance, sh.status, coalesce(to_char(sh.closed_at, 'YYYY-MM-DD HH24:MI'), ''), coalesce(sh.variance_reason, '')
           from public.cashier_shifts sh left join public.stores s on s.id = sh.store_id left join public.users u on u.id = sh.cashier_id
           where {scope} order by sh.opened_at desc""",
        ["Opened", "Store", "Cashier", "Opening cash", "Expected cash", "Counted cash", "Difference", "Status", "Closed", "Reason"],
        scope="sh.store_id = any(:store_ids)",
    ),
    "cash-movements": Kind(
        "pos.shift.view", "Cash in and out",
        """select to_char(m.created_at, 'YYYY-MM-DD HH24:MI'), s.name, u.full_name,
                  case when m.direction = 'in' then 'Cash in' else 'Cash out' end, m.amount, m.reason
           from public.cash_movements m join public.cashier_shifts sh on sh.id = m.shift_id
           left join public.stores s on s.id = sh.store_id left join public.users u on u.id = sh.cashier_id
           where {scope} order by m.created_at desc""",
        ["Time", "Store", "Cashier", "Type", "Amount", "Reason"],
        scope="sh.store_id = any(:store_ids)",
    ),
    "day-close": Kind(
        "pos.shift.view", "Day close",
        """select to_char(d.business_date, 'YYYY-MM-DD'), s.name, d.total_expected_cash, d.total_counted_cash, d.variance,
                  coalesce(u.full_name, ''), coalesce(to_char(d.closed_at, 'YYYY-MM-DD HH24:MI'), '')
           from public.day_close d left join public.stores s on s.id = d.store_id left join public.users u on u.id = d.closed_by
           where {scope} order by d.business_date desc""",
        ["Date", "Store", "Expected cash", "Counted cash", "Difference", "Closed by", "Closed at"],
        scope="d.store_id = any(:store_ids)",
    ),
    "approvals": Kind(
        "approval.request.view", "Approvals",
        """select to_char(a.created_at, 'YYYY-MM-DD HH24:MI'), a.request_type, a.status, ru.full_name, s.name, coalesce(a.reason, ''),
                  coalesce(rv.full_name, ''), coalesce(to_char(a.reviewed_at, 'YYYY-MM-DD HH24:MI'), ''), coalesce(a.review_note, '')
           from public.approval_requests a left join public.users ru on ru.id = a.requested_by left join public.users rv on rv.id = a.reviewed_by
           left join public.stores s on s.id = a.store_id
           where {scope} order by a.created_at desc""",
        ["Requested", "Type", "Status", "Requested by", "Store", "Reason", "Decided by", "Decided at", "Note"],
        scope="a.store_id = any(:store_ids)",
    ),
    "grn": Kind(
        "receiving.grn.view", "Goods received",
        """select coalesce(g.grn_number, upper(left(g.id::text, 8))), to_char(g.created_at, 'YYYY-MM-DD HH24:MI'), coalesce(s.name, w.name),
                  case when g.store_id is not null then 'Store' else 'Warehouse' end, g.status, u.full_name,
                  (select count(*) from public.grn_items gi where gi.grn_id = g.id), coalesce((select sum(gi.received_qty) from public.grn_items gi where gi.grn_id = g.id), 0)
           from public.grn g left join public.stores s on s.id = g.store_id left join public.warehouses w on w.id = g.warehouse_id
           left join public.users u on u.id = g.received_by
           where {scope} order by g.created_at desc""",
        ["GRN", "Date", "Received at", "Type", "Status", "Received by", "Products", "Units received"],
        scope="(g.store_id = any(:store_ids) or g.store_id is null)",
    ),
    "stock-counts": Kind(
        "inventory.stock_count.view", "Stock counts",
        """select to_char(c.created_at, 'YYYY-MM-DD HH24:MI'), s.name, c.status, u.full_name, coalesce(to_char(c.completed_at, 'YYYY-MM-DD HH24:MI'), '')
           from public.stock_counts c left join public.stores s on s.id = c.store_id left join public.users u on u.id = c.initiated_by
           where {scope} order by c.created_at desc""",
        ["Started", "Store", "Status", "Started by", "Finished"],
        scope="c.store_id = any(:store_ids)",
    ),
    "fraud-alerts": Kind(
        "fraud.alert.view", "Fraud alerts",
        """select to_char(f.created_at, 'YYYY-MM-DD HH24:MI'), s.name, f.rule_code, f.severity, f.status, coalesce(f.resolution_note, '')
           from public.fraud_alerts f left join public.stores s on s.id = f.store_id
           where {scope} order by f.created_at desc""",
        ["Raised", "Store", "Rule", "Severity", "Status", "Resolution note"],
        scope="f.store_id = any(:store_ids)",
    ),
    "stock": Kind(
        "inventory.stock.view", "Stock by place",
        """select place, place_code, sku, name, barcode, hsn_code, tax_rate, uom, quantity, purchase_price, round(quantity * purchase_price, 2)
           from (
             select 'Store' as place, s.name as place_name, s.code as place_code, p.sku, p.name, p.barcode, p.hsn_code, p.tax_rate, p.uom,
                    b.quantity, p.purchase_price, s.id as store_id
             from public.inventory_balances b join public.products p on p.id = b.product_id join public.stores s on s.id = b.store_id
             where p.is_active and b.quantity <> 0
             union all
             select 'Warehouse', w.name, w.code, p.sku, p.name, p.barcode, p.hsn_code, p.tax_rate, p.uom, b.quantity, p.purchase_price, null
             from public.warehouse_balances b join public.products p on p.id = b.product_id join public.warehouses w on w.id = b.warehouse_id
             where p.is_active and b.quantity <> 0
           ) x where {scope} order by place, place_name, name""",
        ["Type", "Place code", "SKU", "Product", "Barcode", "HSN", "GST %", "Unit", "Quantity", "Cost price", "Stock value (cost)"],
        scope="(x.store_id = any(:store_ids) or x.store_id is null)",
    ),
}


_STORE_COLUMN = re.compile(r"^([a-z_]+\.[a-z_]+) = any\(:store_ids\)$")


def _scoped_sql(kind: Kind, current: CurrentUser, view_store: uuid.UUID | None = None) -> tuple[str, dict]:
    """Fill the {scope} placeholder: no filter for everyone-sees-all roles and non-store lists, the caller's stores otherwise.
    With view_store, the list is narrowed to that one store (only for lists whose scope is a plain store column)."""
    params: dict = {}
    if not kind.scope or current.sees_all_stores():
        scope = "true"
    else:
        scope = kind.scope
        params["store_ids"] = list(current.store_ids)
    if view_store is not None:
        match = _STORE_COLUMN.match(kind.scope or "")
        if match is None:
            raise HTTPException(status_code=400, detail="This list cannot be narrowed to one store")
        scope = f"({scope}) and {match.group(1)} = :view_store"
        params["view_store"] = view_store
    return kind.sql.replace("{scope}", scope), params


def _spec_for(kind: str, current: CurrentUser) -> Kind:
    spec = KINDS.get(kind)
    if spec is None:
        raise HTTPException(status_code=404, detail="Unknown export")
    if not current.has_permission(spec.permission):
        raise HTTPException(status_code=403, detail=f"Permission denied: {spec.permission}")
    return spec


def _cell(v):
    return float(v) if hasattr(v, "quantize") else (str(v) if isinstance(v, uuid.UUID) else v)


@router.get("/{kind}/view")
async def view_list(
    kind: str,
    store_id: uuid.UUID,
    q: str | None = None,
    limit: int = 10,
    offset: int = 0,
    db: AsyncSession = Depends(get_reporting_db),
    current: CurrentUser = Depends(get_current_user),
) -> dict:
    """The same list as the Excel download, as rows on screen, for one store — used when you look at a store other than
    the till's own. Search matches any column."""
    spec = _spec_for(kind, current)
    require_store_access(store_id, current)
    sql, params = _scoped_sql(spec, current, store_id)
    where = ""
    if q and q.strip():
        where = " where t::text ilike :q"
        params["q"] = f"%{q.strip()}%"
    total = (await db.execute(text(f"select count(*) from ({sql}) t{where}"), params)).scalar_one()
    page = await db.execute(
        text(f"select * from ({sql}) t{where} limit :lim offset :off"),
        {**params, "lim": max(1, min(limit, 200)), "off": max(0, offset)},
    )
    return {"headers": spec.headers, "rows": [[_cell(v) for v in row] for row in page.all()], "total": int(total)}


@router.get("/{kind}")
async def export_list(
    kind: str,
    store_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_reporting_db),
    current: CurrentUser = Depends(get_current_user),
) -> StreamingResponse:
    spec = _spec_for(kind, current)
    if store_id is not None:
        require_store_access(store_id, current)
    sql, params = _scoped_sql(spec, current, store_id)
    result = await db.execute(text(sql), params)
    wb = Workbook()
    ws = wb.active
    ws.title = spec.title[:30]
    ws.append(spec.headers)
    for row in result.all():
        ws.append([_cell(v) for v in row])
    for idx, header in enumerate(spec.headers, start=1):
        ws.column_dimensions[ws.cell(row=1, column=idx).column_letter].width = max(12, min(40, len(header) + 6))
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{kind}.xlsx"'},
    )
