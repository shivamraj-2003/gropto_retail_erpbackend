import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission
from app.core.database import get_db
from app.models.models import Product
from app.schemas.schemas import ProductCreate, ProductOut, ProductPriceChangeRequest, ProductPullResponse
from app.services.approvals import submit_or_apply
from app.services.audit import write_audit

router = APIRouter(prefix="/products", tags=["products"])


@router.get("", response_model=list[ProductOut])
async def list_products(
    q: str | None = None,
    active_only: bool = True,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("inventory.view")),
) -> list[Product]:
    stmt = select(Product)
    if active_only:
        stmt = stmt.where(Product.is_active.is_(True))
    if q:
        stmt = stmt.where(Product.name.ilike(f"%{q}%"))
    result = await db.execute(stmt.limit(200))
    return list(result.scalars().all())


@router.get("/pull", response_model=ProductPullResponse)
async def pull_catalogue(
    since_revision: int = 0,
    page_size: int = 1000,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("inventory.view")),
) -> ProductPullResponse:
    """Master-data pull against a revision watermark, for device catalogue bootstrap/sync."""
    stmt = select(Product).where(Product.revision > since_revision).order_by(Product.revision).limit(page_size)
    rows = list((await db.execute(stmt)).scalars().all())
    watermark = max((p.revision for p in rows), default=since_revision)
    return ProductPullResponse(products=rows, revision_watermark=watermark)


@router.post("", response_model=ProductOut, status_code=201)
async def create_product(
    payload: ProductCreate,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("product.update")),
) -> Product:
    product = Product(**payload.model_dump())
    db.add(product)
    await db.flush()
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=None,
        device_id=current.device_id,
        action="product.created",
        entity_type="product",
        entity_id=product.id,
        new_value=payload.model_dump(mode="json"),
    )
    await db.commit()
    await db.refresh(product)
    return product


@router.post("/{product_id}/price-change")
async def request_price_change(
    product_id: uuid.UUID,
    payload: ProductPriceChangeRequest,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("product.update")),
) -> dict:
    """Price changes are financially sensitive: routed through the approval engine.
    A Super Admin caller applies immediately; anyone else creates a pending request."""
    product = await db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")

    old_value = {"selling_price": float(product.selling_price), "mrp": float(product.mrp)}
    new_value = {k: v for k, v in payload.model_dump(exclude={"reason"}).items() if v is not None}

    request = await submit_or_apply(
        db,
        current=current,
        request_type="price_change",
        entity_type="product",
        entity_id=product_id,
        old_value=old_value,
        new_value=new_value,
        reason=payload.reason,
        store_id=None,
    )
    await db.commit()
    return {"approval_request_id": str(request.id), "status": request.status}


@router.post("/{product_id}/deactivate")
async def request_deactivation(
    product_id: uuid.UUID,
    reason: str | None = None,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("product.deactivate")),
) -> dict:
    product = await db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    request = await submit_or_apply(
        db,
        current=current,
        request_type="product_deactivation",
        entity_type="product",
        entity_id=product_id,
        old_value={"is_active": product.is_active},
        new_value={"is_active": False},
        reason=reason,
        store_id=None,
    )
    await db.commit()
    return {"approval_request_id": str(request.id), "status": request.status}
