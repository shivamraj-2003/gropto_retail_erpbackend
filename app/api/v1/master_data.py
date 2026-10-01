"""Blueprint §15 master data: company/legal-entity, cluster/region, department,
payment-mode and reason-code catalogues, and chart of accounts. These tables
(models_phase4.py) existed in code but had no API surface at all — this router
closes that gap. Central master governance (§21): only config.manage can
write; every authenticated user with inventory.view can read, since these are
low-sensitivity lookups referenced all over the app (reason codes on stock
adjustments, payment modes on the till, etc.)."""

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission
from app.core.database import get_db
from app.models.models_phase4 import ChartOfAccount, Cluster, Company, Department, PaymentModeMaster, ReasonCodeMaster
from app.schemas.schemas_phase4 import (
    ChartOfAccountIn,
    ChartOfAccountOut,
    ClusterIn,
    ClusterOut,
    CompanyIn,
    CompanyOut,
    DepartmentIn,
    DepartmentOut,
    PaymentModeIn,
    PaymentModeOut,
    ReasonCodeIn,
    ReasonCodeOut,
)
from app.services.audit import write_audit

router = APIRouter(prefix="/master-data", tags=["master-data"])


@router.get("/companies", response_model=list[CompanyOut])
async def list_companies(
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("inventory.view")),
) -> list[Company]:
    return list((await db.execute(select(Company).order_by(Company.name))).scalars().all())


@router.post("/companies", response_model=CompanyOut, status_code=201)
async def create_company(
    payload: CompanyIn,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("finance.gst_configure")),
) -> Company:
    company = Company(**payload.model_dump())
    db.add(company)
    await db.flush()
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=None,
        device_id=current.device_id,
        action="company.created",
        entity_type="company",
        entity_id=company.id,
        new_value=payload.model_dump(mode="json"),
    )
    await db.commit()
    await db.refresh(company)
    return company


@router.put("/companies/{company_id}", response_model=CompanyOut)
async def update_company(
    company_id: uuid.UUID,
    payload: CompanyIn,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("finance.gst_configure")),
) -> Company:
    """Point 10 audit fix: GSTIN/PAN/e-invoice applicability changes had no
    audited update path at all (POST-only, no PUT) — GST config changes are
    exactly the kind of change that must be auditable."""
    from fastapi import HTTPException

    company = await db.get(Company, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Company not found")
    old_value = {
        "gstin": company.gstin,
        "pan": company.pan,
        "einvoice_applicable": company.einvoice_applicable,
        "aato_threshold": float(company.aato_threshold) if company.aato_threshold is not None else None,
    }
    for field, value in payload.model_dump().items():
        setattr(company, field, value)
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=None,
        device_id=current.device_id,
        action="company.updated",
        entity_type="company",
        entity_id=company.id,
        old_value=old_value,
        new_value=payload.model_dump(mode="json"),
    )
    await db.commit()
    await db.refresh(company)
    return company


@router.get("/clusters", response_model=list[ClusterOut])
async def list_clusters(
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("inventory.view")),
) -> list[Cluster]:
    return list((await db.execute(select(Cluster).order_by(Cluster.name))).scalars().all())


@router.post("/clusters", response_model=ClusterOut, status_code=201)
async def create_cluster(
    payload: ClusterIn,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("config.manage")),
) -> Cluster:
    cluster = Cluster(**payload.model_dump())
    db.add(cluster)
    await db.commit()
    await db.refresh(cluster)
    return cluster


@router.put("/clusters/{cluster_id}", response_model=ClusterOut)
async def update_cluster(
    cluster_id: uuid.UUID,
    payload: ClusterIn,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("config.manage")),
) -> Cluster:
    cluster = await db.get(Cluster, cluster_id)
    if cluster is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Cluster not found")
    for field, value in payload.model_dump().items():
        setattr(cluster, field, value)
    await db.commit()
    await db.refresh(cluster)
    return cluster


@router.get("/departments", response_model=list[DepartmentOut])
async def list_departments(
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("inventory.view")),
) -> list[Department]:
    return list((await db.execute(select(Department).order_by(Department.name))).scalars().all())


@router.post("/departments", response_model=DepartmentOut, status_code=201)
async def create_department(
    payload: DepartmentIn,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("config.manage")),
) -> Department:
    department = Department(**payload.model_dump())
    db.add(department)
    await db.commit()
    await db.refresh(department)
    return department


@router.get("/payment-modes", response_model=list[PaymentModeOut])
async def list_payment_modes(
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("inventory.view")),
) -> list[PaymentModeMaster]:
    return list((await db.execute(select(PaymentModeMaster).order_by(PaymentModeMaster.name))).scalars().all())


@router.post("/payment-modes", response_model=PaymentModeOut, status_code=201)
async def create_payment_mode(
    payload: PaymentModeIn,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("config.manage")),
) -> PaymentModeMaster:
    mode = PaymentModeMaster(**payload.model_dump())
    db.add(mode)
    await db.commit()
    await db.refresh(mode)
    return mode


@router.get("/reason-codes", response_model=list[ReasonCodeOut])
async def list_reason_codes(
    category: str | None = None,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("inventory.view")),
) -> list[ReasonCodeMaster]:
    stmt = select(ReasonCodeMaster).order_by(ReasonCodeMaster.category, ReasonCodeMaster.code)
    if category:
        stmt = stmt.where(ReasonCodeMaster.category == category)
    return list((await db.execute(stmt)).scalars().all())


@router.post("/reason-codes", response_model=ReasonCodeOut, status_code=201)
async def create_reason_code(
    payload: ReasonCodeIn,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("config.manage")),
) -> ReasonCodeMaster:
    reason = ReasonCodeMaster(**payload.model_dump())
    db.add(reason)
    await db.commit()
    await db.refresh(reason)
    return reason


@router.get("/chart-of-accounts", response_model=list[ChartOfAccountOut])
async def list_chart_of_accounts(
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("report.export")),
) -> list[ChartOfAccount]:
    return list((await db.execute(select(ChartOfAccount).order_by(ChartOfAccount.account_code))).scalars().all())


@router.post("/chart-of-accounts", response_model=ChartOfAccountOut, status_code=201)
async def create_chart_of_account(
    payload: ChartOfAccountIn,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("config.manage")),
) -> ChartOfAccount:
    account = ChartOfAccount(**payload.model_dump())
    db.add(account)
    await db.commit()
    await db.refresh(account)
    return account
