"""Small helpers for product master data."""

from datetime import datetime, timezone

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession


async def bump_revision(db: AsyncSession, row) -> int:
    """Give a pulled record (product, discount rule) a fresh revision.

    Tills fetch "everything with revision greater than the last one I saw", and
    revisions come from one shared counter. Adding 1 to the row's own old number
    lands below what the tills already saw, so the edit never reached them; this
    takes the next number from the shared counter instead.
    """
    revision = (await db.execute(text("select nextval('global_revision_seq')"))).scalar_one()
    row.revision = revision
    if hasattr(row, "updated_at"):
        row.updated_at = datetime.now(timezone.utc)
    return revision


def clean_barcodes(primary: str | None, extras: list[str] | None) -> tuple[str | None, list[str]]:
    """Trim, drop blanks and repeats. The main barcode is never repeated among the extras."""
    main = (primary or "").strip() or None
    seen = {main} if main else set()
    out: list[str] = []
    for raw in extras or []:
        code = (raw or "").strip()
        if code and code not in seen:
            seen.add(code)
            out.append(code)
    return main, out


async def barcode_owner(db: AsyncSession, code: str):
    """The product id that already uses this barcode (main or extra), or None."""
    from app.models.models import Product, ProductBarcode

    owner = (await db.execute(select(Product.id).where(Product.barcode == code))).scalar_one_or_none()
    if owner is not None:
        return owner
    return (await db.execute(select(ProductBarcode.product_id).where(ProductBarcode.barcode == code))).scalar_one_or_none()


async def product_has_barcode(db: AsyncSession, product, code: str) -> bool:
    """True when `code` is the product's main barcode or one of its extras."""
    if product is None:
        return False
    if product.barcode == code:
        return True
    return any(b.barcode == code for b in product.alt_barcodes)
