import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission, require_store_access
from app.core.database import get_db
from app.models.models import Customer, Sale, SaleItem
from app.models.models_phase2 import Return, ReturnItem
from app.schemas.schemas import Page
from app.schemas.schemas_phase2 import ReturnCreate, ReturnLinkExchangeIn, ReturnOut
from app.services.returns import create_return, link_exchange

router = APIRouter(prefix="/returns", tags=["returns"])


@router.get("", response_model=Page[ReturnOut])
async def list_returns(
    store_id: uuid.UUID | None = None,
    limit: int = 20,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("pos.return.view")),
) -> Page[ReturnOut]:
    stmt = select(Return)
    if not current.sees_all_stores():
        stmt = stmt.where(Return.store_id.in_(current.store_ids))
    if store_id:
        require_store_access(store_id, current)
        stmt = stmt.where(Return.store_id == store_id)
    capped_limit = min(limit, 200)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    result = await db.execute(stmt.order_by(Return.created_at.desc()).limit(capped_limit).offset(offset))
    return Page(items=list(result.scalars().all()), total=total, limit=capped_limit, offset=offset)


class FoundBill(BaseModel):
    id: uuid.UUID
    store_id: uuid.UUID
    bill_number: str
    billed_at: str
    grand_total: float
    customer_name: str | None
    customer_phone: str | None
    items: list[str]


class BillItemOut(BaseModel):
    id: uuid.UUID
    product_id: uuid.UUID
    name: str
    quantity: float
    returned: float
    remaining: float
    unit_price: float
    paid_unit: float  # what the customer really paid per unit (after line and whole-bill discounts)


class BillOut(BaseModel):
    id: uuid.UUID
    store_id: uuid.UUID
    bill_number: str
    billed_at: str
    grand_total: float
    customer_name: str | None
    customer_phone: str | None
    items: list[BillItemOut]


@router.get("/find-bill", response_model=list[FoundBill])
async def find_bill(
    q: str = "",
    store_id: uuid.UUID | None = None,
    limit: int = 20,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("pos.return.refund")),
) -> list[FoundBill]:
    """Finds the bill for a return when the customer has lost it: by bill number, mobile number (even just the last
    digits), name, or the name of something they bought. Looks at every till of the store, not just this one."""
    text = q.strip()
    if len(text) < 2:
        return []
    stmt = (
        select(Sale, Customer.name, Customer.phone)
        .outerjoin(Customer, Customer.id == Sale.customer_id)
        .where(Sale.status == "completed")
    )
    if not current.sees_all_stores():
        stmt = stmt.where(Sale.store_id.in_(current.store_ids))
    if store_id:
        require_store_access(store_id, current)
        stmt = stmt.where(Sale.store_id == store_id)
    like = f"%{text}%"
    bought = exists().where(SaleItem.sale_id == Sale.id, SaleItem.product_name_snapshot.ilike(like))
    stmt = stmt.where(or_(Sale.bill_number.ilike(like), Customer.phone.ilike(like), Customer.name.ilike(like), bought))
    rows = (await db.execute(stmt.order_by(Sale.billed_at.desc()).limit(min(max(limit, 1), 50)))).all()
    return [
        FoundBill(
            id=sale.id,
            store_id=sale.store_id,
            bill_number=sale.bill_number,
            billed_at=sale.billed_at.isoformat(),
            grand_total=float(sale.grand_total),
            customer_name=name,
            customer_phone=phone,
            items=[i.product_name_snapshot for i in sale.items][:4],
        )
        for sale, name, phone in rows
    ]


@router.get("/browse-bills")
async def browse_bills(
    store_id: uuid.UUID | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    q: str = "",
    limit: int = 20,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("pos.return.refund")),
) -> dict:
    """Past bills, newest first, one page at a time, for any date range (India calendar days). `q` is optional and matches
    bill number, mobile, name or an item bought. For looking through bills before choosing one to return."""
    limit = min(max(limit, 1), 50)
    offset = max(offset, 0)
    stmt = (
        select(Sale, Customer.name, Customer.phone)
        .outerjoin(Customer, Customer.id == Sale.customer_id)
        .where(Sale.status == "completed")
    )
    if not current.sees_all_stores():
        stmt = stmt.where(Sale.store_id.in_(current.store_ids))
    if store_id:
        require_store_access(store_id, current)
        stmt = stmt.where(Sale.store_id == store_id)
    day = func.date(func.timezone("Asia/Kolkata", Sale.billed_at))
    if date_from:
        stmt = stmt.where(day >= date_from)
    if date_to:
        stmt = stmt.where(day <= date_to)
    text = q.strip()
    if len(text) >= 2:
        like = f"%{text}%"
        bought = exists().where(SaleItem.sale_id == Sale.id, SaleItem.product_name_snapshot.ilike(like))
        stmt = stmt.where(or_(Sale.bill_number.ilike(like), Customer.phone.ilike(like), Customer.name.ilike(like), bought))
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (await db.execute(stmt.order_by(Sale.billed_at.desc(), Sale.id).limit(limit).offset(offset))).all()
    return {
        "total": int(total),
        "limit": limit,
        "offset": offset,
        "items": [
            FoundBill(
                id=sale.id,
                store_id=sale.store_id,
                bill_number=sale.bill_number,
                billed_at=sale.billed_at.isoformat(),
                grand_total=float(sale.grand_total),
                customer_name=name,
                customer_phone=phone,
                items=[i.product_name_snapshot for i in sale.items][:4],
            ).model_dump(mode="json")
            for sale, name, phone in rows
        ],
    }


@router.get("/bill/{sale_id}", response_model=BillOut)
async def bill_for_return(
    sale_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("pos.return.refund")),
) -> BillOut:
    """A bill as the server holds it, with its own item ids and how much of each line can still come back."""
    sale = await db.get(Sale, sale_id)
    if sale is None:
        raise HTTPException(status_code=404, detail="Bill not found on the server (it may not have synced yet)")
    require_store_access(sale.store_id, current)
    customer = await db.get(Customer, sale.customer_id) if sale.customer_id else None
    ids = [i.id for i in sale.items]
    returned: dict[uuid.UUID, float] = {}
    if ids:
        rows = (
            await db.execute(
                select(ReturnItem.sale_item_id, func.coalesce(func.sum(ReturnItem.quantity), 0))
                .join(Return, Return.id == ReturnItem.return_id)
                .where(ReturnItem.sale_item_id.in_(ids), Return.status != "rejected")
                .group_by(ReturnItem.sale_item_id)
            )
        ).all()
        returned = {sid: float(q) for sid, q in rows}
    line_sum = sum(float(i.line_total) for i in sale.items)
    ratio = min(float(sale.grand_total) / line_sum, 1.0) if line_sum > 0 else 1.0
    items = []
    for i in sale.items:
        qty = float(i.quantity)
        done = returned.get(i.id, 0.0)
        items.append(
            BillItemOut(
                id=i.id,
                product_id=i.product_id,
                name=i.product_name_snapshot,
                quantity=qty,
                returned=done,
                remaining=max(qty - done, 0.0),
                unit_price=float(i.unit_price),
                paid_unit=round(float(i.line_total) / qty * ratio, 2) if qty else 0.0,
            )
        )
    return BillOut(
        id=sale.id,
        store_id=sale.store_id,
        bill_number=sale.bill_number,
        billed_at=sale.billed_at.isoformat(),
        grand_total=float(sale.grand_total),
        customer_name=customer.name if customer else None,
        customer_phone=customer.phone if customer else None,
        items=items,
    )


@router.post("", response_model=ReturnOut, status_code=201)
async def submit_return(
    payload: ReturnCreate,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("pos.return.refund")),
) -> Return:
    require_store_access(payload.store_id, current)
    ret = await create_return(db, current=current, payload=payload)
    await db.commit()
    await db.refresh(ret)
    return ret


@router.post("/{return_id}/link-exchange", response_model=ReturnOut)
async def link_return_exchange(
    return_id: uuid.UUID,
    payload: ReturnLinkExchangeIn,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("pos.return.update")),
) -> Return:
    ret = await db.get(Return, return_id)
    if ret is None:
        raise HTTPException(status_code=404, detail="Return not found")
    require_store_access(ret.store_id, current)
    ret = await link_exchange(db, current=current, ret=ret, exchange_sale_id=payload.exchange_sale_id)
    await db.commit()
    await db.refresh(ret)
    return ret
