"""Recording a vendor's purchase bill. One place for it, used by the Purchase Entry screen and by the purchase-bill file import.

Each line is the vendor bill's own line: receipt, free, reject and breakage quantities, rate, discount, GST code, cess and
surcharge. The server works out every amount itself. Stock goes up by receipt + free - reject - breakage, at the store or, when a
line names a warehouse, in that warehouse.
"""

import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser
from app.models.models import InventoryBalance, PriceVariantStock, Product, Purchase, PurchaseItem
from app.models.models_phase4 import InventoryBatch
from app.schemas.schemas import PurchaseCreate
from app.services.approvals import submit_or_apply
from app.services.audit import write_audit
from app.services.catalog import bump_revision
from app.services.inventory import adjust_warehouse_balance, apply_movement
from app.services.purchase_bill import ALLOWED_GST_RATES, line_amounts, stock_in
from app.services.purchase_prices import default_selling_price, mrp_differs, units_at_current_price


async def record_purchase(db: AsyncSession, payload: PurchaseCreate, current: CurrentUser, *, source: str = "entry") -> Purchase:
    if not payload.items:
        raise HTTPException(status_code=422, detail="Add at least one line")
    products = {
        p.id: p
        for p in (await db.execute(select(Product).where(Product.id.in_({i.product_id for i in payload.items})))).scalars().all()
    }

    rows: list[tuple] = []  # (item, product, gst_rate, amounts, stock_in)
    for n, item in enumerate(payload.items, start=1):
        product = products.get(item.product_id)
        if product is None:
            raise HTTPException(status_code=404, detail=f"Line {n}: that product no longer exists")
        gst_rate = float(item.gst_rate) if item.gst_rate is not None else float(product.tax_rate)
        if gst_rate not in ALLOWED_GST_RATES:
            raise HTTPException(status_code=422, detail=f"Line {n} ({product.name}): GST {gst_rate:g}% is not a valid rate")
        into_stock = stock_in(item.quantity, item.free_qty, item.reject_qty, item.hl_qty)
        if into_stock < 0:
            raise HTTPException(status_code=422, detail=f"Line {n} ({product.name}): rejected + broken goods are more than was received")
        if item.warehouse_id is not None and not current.owns_warehouse(item.warehouse_id):
            raise HTTPException(status_code=403, detail=f"Line {n} ({product.name}): you cannot receive into that warehouse")
        amounts = line_amounts(
            receipt=item.quantity, rate=item.unit_cost, discount=item.discount_amount, gst_rate=gst_rate,
            cess=item.cess_amount, surcharge_rate=item.surcharge_rate,
        )
        if amounts["taxable"] < 0:
            raise HTTPException(status_code=422, detail=f"Line {n} ({product.name}): the discount is more than the amount")
        if item.mrp_action != "keep":
            if item.warehouse_id is not None:
                raise HTTPException(status_code=422, detail=f"Line {n} ({product.name}): a price change applies to store stock, not to a warehouse line")
            if item.mrp is None or not mrp_differs(item.mrp, float(product.mrp)):
                raise HTTPException(status_code=422, detail=f"Line {n} ({product.name}): the MRP is the same as the product's, so there is no price to change")
            if not current.has_permission("pricing.price.update"):
                raise HTTPException(status_code=403, detail=f"Line {n} ({product.name}): you are not allowed to change prices")
            sell = item.new_selling_price or default_selling_price(float(product.mrp), float(product.selling_price), item.mrp)
            if sell > item.mrp + 0.005:
                raise HTTPException(status_code=422, detail=f"Line {n} ({product.name}): the selling price cannot be above the MRP")
        rows.append((item, product, gst_rate, amounts, into_stock))

    if payload.update_cost and not current.has_permission("catalog.product.create"):
        raise HTTPException(status_code=403, detail="You are not allowed to change product cost prices")

    total = sum((r[3]["net"] for r in rows), 0)
    purchase = Purchase(
        vendor_id=payload.vendor_id,
        store_id=payload.store_id,
        invoice_number=payload.invoice_number,
        invoice_date=payload.invoice_date,
        total_amount=total,
        created_by=current.user_id,
    )
    db.add(purchase)
    await db.flush()

    notes: list[str] = []
    await _apply_price_actions(db, rows, payload, current, notes)

    store_stock: dict[uuid.UUID, float] = {}
    warehouse_stock: dict[tuple[uuid.UUID, uuid.UUID], float] = {}
    for item, product, gst_rate, amounts, into_stock in rows:
        batch_no = (item.batch_no or "").strip()
        db.add(
            PurchaseItem(
                purchase_id=purchase.id, product_id=item.product_id, quantity=item.quantity, unit_cost=item.unit_cost,
                bill_serial=item.bill_serial, mrp=item.mrp, batch_no=batch_no or None, hsn_code=item.hsn_code or product.hsn_code,
                free_qty=item.free_qty, reject_qty=item.reject_qty, hl_qty=item.hl_qty, rate_unit=item.rate_unit,
                discount_amount=item.discount_amount, gst_rate=gst_rate, gst_amount=amounts["gst"], cess_amount=amounts["cess"],
                surcharge_rate=item.surcharge_rate, surcharge_amount=amounts["surcharge"], net_amount=amounts["net"],
                warehouse_id=item.warehouse_id,
            )
        )
        if item.warehouse_id is None:
            store_stock[item.product_id] = store_stock.get(item.product_id, 0) + float(into_stock)
        else:
            key = (item.product_id, item.warehouse_id)
            warehouse_stock[key] = warehouse_stock.get(key, 0) + float(into_stock)

        # A batch number puts this lot in the batch ledger (of the store, or of the warehouse it went to). Free goods lower the cost per unit.
        if batch_no and into_stock > 0:
            unit_cost = float(amounts["taxable"] / into_stock)
            where = (
                (InventoryBatch.store_id == payload.store_id, InventoryBatch.warehouse_id.is_(None))
                if item.warehouse_id is None
                else (InventoryBatch.warehouse_id == item.warehouse_id,)
            )
            batch = await db.scalar(
                select(InventoryBatch).where(InventoryBatch.product_id == item.product_id, InventoryBatch.batch_number == batch_no, *where)
            )
            if batch is None:
                db.add(
                    InventoryBatch(
                        product_id=item.product_id, store_id=payload.store_id if item.warehouse_id is None else None,
                        warehouse_id=item.warehouse_id, batch_number=batch_no, quantity=float(into_stock), purchase_cost=unit_cost,
                    )
                )
            else:
                batch.quantity = float(batch.quantity) + float(into_stock)
                batch.purchase_cost = unit_cost

    # One stock movement per product per bill (the same product on two lines, for example two batches, is added together)
    for product_id, qty in store_stock.items():
        if qty == 0:
            continue
        await apply_movement(
            db, product_id=product_id, store_id=payload.store_id, delta=qty, reason_code="purchase_receipt",
            source_type="purchase_item", source_id=purchase.id, created_by=current.user_id, device_id=current.device_id,
        )
    for (product_id, warehouse_id), qty in warehouse_stock.items():
        if qty != 0:
            await adjust_warehouse_balance(db, product_id=product_id, warehouse_id=warehouse_id, delta=qty)

    if payload.update_cost:
        await _update_costs(db, rows, current, notes, purchase.id)

    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=payload.store_id,
        device_id=current.device_id,
        action="purchase.created",
        entity_type="purchase",
        entity_id=purchase.id,
        new_value={"total_amount": float(total), "vendor_id": str(payload.vendor_id), "lines": len(rows), "source": source},
    )
    purchase.notes = notes  # shown to the person who entered it; not stored
    return purchase


def _state(status: str) -> str:
    return "waiting for approval" if status == "pending" else "done"


async def _apply_price_actions(db: AsyncSession, rows: list[tuple], payload: PurchaseCreate, current: CurrentUser, notes: list[str]) -> None:
    """Lines whose MRP is not the product's: make the price change the person chose, through the approval engine."""
    handled: set[uuid.UUID] = set()
    for item, product, _gst, amounts, into_stock in rows:
        if item.mrp_action == "keep":
            continue
        if product.id in handled:
            notes.append(f"{product.name}: price already handled by an earlier line of this bill")
            continue
        handled.add(product.id)
        new_mrp, old_mrp, old_sell = float(item.mrp), float(product.mrp), float(product.selling_price)
        sell = float(item.new_selling_price or default_selling_price(old_mrp, old_sell, new_mrp))
        balance = await db.get(InventoryBalance, (product.id, payload.store_id))
        total = float(balance.quantity) if balance else 0.0
        active = [v for v in product.price_variants if v.is_active]
        shares = {}
        for v in active:
            row = await db.get(PriceVariantStock, (v.id, payload.store_id))
            shares[v.id] = float(row.quantity) if row else 0.0

        if item.mrp_action == "new_stock_price":
            # only the units on this bill carry the new MRP
            if into_stock <= 0:
                continue
            same = next((v for v in active if abs(float(v.mrp) - new_mrp) < 0.005 and abs(float(v.selling_price) - sell) < 0.005), None)
            if same is not None:
                row = await db.get(PriceVariantStock, (same.id, payload.store_id))
                if row is None:
                    db.add(PriceVariantStock(variant_id=same.id, store_id=payload.store_id, quantity=float(into_stock)))
                else:
                    row.quantity = float(row.quantity) + float(into_stock)
                notes.append(f"{product.name}: {float(into_stock):g} unit(s) added to its existing price MRP {new_mrp:g}")
                continue
            req = await submit_or_apply(
                db, current=current, request_type="price_variant_add", entity_type="product", entity_id=product.id,
                old_value={"selling_price": old_sell, "mrp": old_mrp},
                new_value={
                    "mrp": new_mrp, "selling_price": sell, "purchase_price": float(amounts["taxable"] / into_stock),
                    "label": f"MRP {new_mrp:g}", "store_id": str(payload.store_id), "quantity": float(into_stock),
                },
                reason=f"Purchase {payload.invoice_number or ''} at a different MRP".strip(), store_id=None,
            )
            notes.append(f"{product.name}: {float(into_stock):g} unit(s) at MRP {new_mrp:g}, selling {sell:g} ({_state(req.status)})")
        else:
            # new_price: the new MRP is now the price; what the store already holds keeps the old one
            at_old = units_at_current_price(total, list(shares.values()))
            if at_old > 0:
                await submit_or_apply(
                    db, current=current, request_type="price_variant_add", entity_type="product", entity_id=product.id,
                    old_value={"selling_price": old_sell, "mrp": old_mrp},
                    new_value={
                        "mrp": old_mrp, "selling_price": old_sell, "purchase_price": float(product.purchase_price or 0),
                        "label": "Old stock", "store_id": str(payload.store_id), "quantity": at_old,
                    },
                    reason=f"Purchase {payload.invoice_number or ''} raised the MRP".strip(), store_id=None,
                )
            req = await submit_or_apply(
                db, current=current, request_type="price_change", entity_type="product", entity_id=product.id,
                old_value={"selling_price": old_sell, "mrp": old_mrp}, new_value={"mrp": new_mrp, "selling_price": sell},
                reason=f"Purchase {payload.invoice_number or ''} at a new MRP".strip(), store_id=None,
            )
            keep = f"; the {at_old:g} unit(s) already held keep MRP {old_mrp:g}" if at_old > 0 else ""
            notes.append(f"{product.name}: price becomes MRP {new_mrp:g}, selling {sell:g} ({_state(req.status)}){keep}")


async def _update_costs(db: AsyncSession, rows: list[tuple], current: CurrentUser, notes: list[str], purchase_id: uuid.UUID) -> None:
    """Set each product's cost price to what this bill cost per unit before GST, free goods counted in (the last line of a
    product wins). Cost price is not an approval-gated field, so this is the same as editing it on the product."""
    last: dict[uuid.UUID, tuple] = {}
    for item, product, _gst, amounts, into_stock in rows:
        cost = float(amounts["taxable"] / into_stock) if into_stock > 0 else float(item.unit_cost)
        last[product.id] = (product, round(cost, 2))
    for product, cost in last.values():
        old = float(product.purchase_price or 0)
        if abs(old - cost) < 0.005:
            continue
        product.purchase_price = cost
        await bump_revision(db, product)
        await write_audit(
            db, user_id=current.user_id, role_code=current.role_code, store_id=None, device_id=current.device_id,
            action="product.cost_updated", entity_type="product", entity_id=product.id,
            old_value={"purchase_price": old}, new_value={"purchase_price": cost, "purchase_id": str(purchase_id)},
        )
        notes.append(f"{product.name}: cost price updated {old:g} to {cost:g}")
