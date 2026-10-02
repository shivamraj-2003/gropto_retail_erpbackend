"""Blueprint §15 master data: company/legal-entity, region/cluster, department,
payment-mode and reason-code catalogues, and chart of accounts. These tables
(models_phase4.py) existed in code but had no API surface at all — this router
closes that gap. Central master governance (§21): only config.manage can
write; every authenticated user with inventory.view can read, since these are
low-sensitivity lookups referenced all over the app (reason codes on stock
adjustments, payment modes on the till, etc.).

Point 15 audit fix: every entity below previously had create-only (or, for
Region, no) API coverage — no update/deactivate path, and (except Company)
no audit logging at all. Every entity now has a real update endpoint that
can also flip is_active, and every create/update call is audited the same
way Company's always was."""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission
from app.core.database import get_db
from app.models.models import Region
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
    RegionIn,
    RegionOut,
)
from app.services.audit import write_audit

router = APIRouter(prefix="/master-data", tags=["master-data"])


@router.get("/companies", response_model=list[CompanyOut])
async def list_companies(
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("masterdata.company.view")),
) -> list[Company]:
    return list((await db.execute(select(Company).order_by(Company.name))).scalars().all())


@router.post("/companies", response_model=CompanyOut, status_code=201)
async def create_company(
    payload: CompanyIn,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("masterdata.company.create")),
) -> Company:
    existing = (await db.execute(select(Company).where(Company.gstin == payload.gstin))).scalar_one_or_none() if payload.gstin else None
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"A company with GSTIN '{payload.gstin}' already exists")
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
    current: CurrentUser = Depends(require_permission("masterdata.company.update")),
) -> Company:
    """Point 10 audit fix: GSTIN/PAN/e-invoice applicability changes had no
    audited update path at all (POST-only, no PUT) — GST config changes are
    exactly the kind of change that must be auditable. Point 15 audit fix:
    is_active now flows through here too, so a legal entity can be retired."""
    company = await db.get(Company, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Company not found")
    if payload.gstin and payload.gstin != company.gstin:
        existing = (await db.execute(select(Company).where(Company.gstin == payload.gstin, Company.id != company_id))).scalar_one_or_none()
        if existing is not None:
            raise HTTPException(status_code=409, detail=f"A company with GSTIN '{payload.gstin}' already exists")
    old_value = {
        "gstin": company.gstin,
        "pan": company.pan,
        "einvoice_applicable": company.einvoice_applicable,
        "aato_threshold": float(company.aato_threshold) if company.aato_threshold is not None else None,
        "is_active": company.is_active,
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


@router.get("/regions", response_model=list[RegionOut])
async def list_regions(
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("masterdata.region.view")),
) -> list[Region]:
    """Point 15 audit fix: Region had a model and a live FK from Store.region_id
    but no API/UI anywhere — nothing could ever create a row here except a
    seed script. This is the first write path it has ever had."""
    return list((await db.execute(select(Region).order_by(Region.name))).scalars().all())


@router.post("/regions", response_model=RegionOut, status_code=201)
async def create_region(
    payload: RegionIn,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("masterdata.region.create")),
) -> Region:
    existing = (await db.execute(select(Region).where(Region.code == payload.code))).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"Region code '{payload.code}' already exists")
    region = Region(**payload.model_dump())
    db.add(region)
    await db.flush()
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=None,
        device_id=current.device_id,
        action="region.created",
        entity_type="region",
        entity_id=region.id,
        new_value=payload.model_dump(mode="json"),
    )
    await db.commit()
    await db.refresh(region)
    return region


@router.put("/regions/{region_id}", response_model=RegionOut)
async def update_region(
    region_id: uuid.UUID,
    payload: RegionIn,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("masterdata.region.update")),
) -> Region:
    region = await db.get(Region, region_id)
    if region is None:
        raise HTTPException(status_code=404, detail="Region not found")
    if payload.code != region.code:
        existing = (await db.execute(select(Region).where(Region.code == payload.code, Region.id != region_id))).scalar_one_or_none()
        if existing is not None:
            raise HTTPException(status_code=409, detail=f"Region code '{payload.code}' already exists")
    old_value = {"name": region.name, "code": region.code, "is_active": region.is_active}
    for field, value in payload.model_dump().items():
        setattr(region, field, value)
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=None,
        device_id=current.device_id,
        action="region.updated",
        entity_type="region",
        entity_id=region.id,
        old_value=old_value,
        new_value=payload.model_dump(mode="json"),
    )
    await db.commit()
    await db.refresh(region)
    return region


@router.get("/clusters", response_model=list[ClusterOut])
async def list_clusters(
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("masterdata.cluster.view")),
) -> list[Cluster]:
    return list((await db.execute(select(Cluster).order_by(Cluster.name))).scalars().all())


@router.post("/clusters", response_model=ClusterOut, status_code=201)
async def create_cluster(
    payload: ClusterIn,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("masterdata.cluster.create")),
) -> Cluster:
    existing = (await db.execute(select(Cluster).where(Cluster.region_code == payload.region_code))).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"Region code '{payload.region_code}' is already assigned to another cluster")
    cluster = Cluster(**payload.model_dump())
    db.add(cluster)
    await db.flush()
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=None,
        device_id=current.device_id,
        action="cluster.created",
        entity_type="cluster",
        entity_id=cluster.id,
        new_value=payload.model_dump(mode="json"),
    )
    await db.commit()
    await db.refresh(cluster)
    return cluster


@router.put("/clusters/{cluster_id}", response_model=ClusterOut)
async def update_cluster(
    cluster_id: uuid.UUID,
    payload: ClusterIn,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("masterdata.cluster.update")),
) -> Cluster:
    cluster = await db.get(Cluster, cluster_id)
    if cluster is None:
        raise HTTPException(status_code=404, detail="Cluster not found")
    if payload.region_code != cluster.region_code:
        existing = (
            await db.execute(select(Cluster).where(Cluster.region_code == payload.region_code, Cluster.id != cluster_id))
        ).scalar_one_or_none()
        if existing is not None:
            raise HTTPException(status_code=409, detail=f"Region code '{payload.region_code}' is already assigned to another cluster")
    old_value = {
        "name": cluster.name,
        "region_code": cluster.region_code,
        "regional_manager_id": str(cluster.regional_manager_id) if cluster.regional_manager_id else None,
        "is_active": cluster.is_active,
    }
    for field, value in payload.model_dump().items():
        setattr(cluster, field, value)
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=None,
        device_id=current.device_id,
        action="cluster.updated",
        entity_type="cluster",
        entity_id=cluster.id,
        old_value=old_value,
        new_value=payload.model_dump(mode="json"),
    )
    await db.commit()
    await db.refresh(cluster)
    return cluster


@router.get("/departments", response_model=list[DepartmentOut])
async def list_departments(
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("masterdata.department.view")),
) -> list[Department]:
    return list((await db.execute(select(Department).order_by(Department.name))).scalars().all())


@router.post("/departments", response_model=DepartmentOut, status_code=201)
async def create_department(
    payload: DepartmentIn,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("masterdata.department.create")),
) -> Department:
    existing = (await db.execute(select(Department).where(Department.code == payload.code))).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"Department code '{payload.code}' already exists")
    department = Department(**payload.model_dump())
    db.add(department)
    await db.flush()
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=None,
        device_id=current.device_id,
        action="department.created",
        entity_type="department",
        entity_id=department.id,
        new_value=payload.model_dump(mode="json"),
    )
    await db.commit()
    await db.refresh(department)
    return department


@router.put("/departments/{department_id}", response_model=DepartmentOut)
async def update_department(
    department_id: uuid.UUID,
    payload: DepartmentIn,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("masterdata.department.update")),
) -> Department:
    """Point 15 audit fix: no update endpoint existed at all — a typo in a
    department name or code could never be fixed without direct DB access,
    and is_active had no way to be set to False."""
    department = await db.get(Department, department_id)
    if department is None:
        raise HTTPException(status_code=404, detail="Department not found")
    if payload.code != department.code:
        existing = (
            await db.execute(select(Department).where(Department.code == payload.code, Department.id != department_id))
        ).scalar_one_or_none()
        if existing is not None:
            raise HTTPException(status_code=409, detail=f"Department code '{payload.code}' already exists")
    old_value = {"name": department.name, "code": department.code, "is_active": department.is_active}
    for field, value in payload.model_dump().items():
        setattr(department, field, value)
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=None,
        device_id=current.device_id,
        action="department.updated",
        entity_type="department",
        entity_id=department.id,
        old_value=old_value,
        new_value=payload.model_dump(mode="json"),
    )
    await db.commit()
    await db.refresh(department)
    return department


@router.get("/payment-modes", response_model=list[PaymentModeOut])
async def list_payment_modes(
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("masterdata.payment_mode.view")),
) -> list[PaymentModeMaster]:
    return list((await db.execute(select(PaymentModeMaster).order_by(PaymentModeMaster.name))).scalars().all())


@router.post("/payment-modes", response_model=PaymentModeOut, status_code=201)
async def create_payment_mode(
    payload: PaymentModeIn,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("masterdata.payment_mode.create")),
) -> PaymentModeMaster:
    existing = (await db.execute(select(PaymentModeMaster).where(PaymentModeMaster.code == payload.code))).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"Payment mode code '{payload.code}' already exists")
    mode = PaymentModeMaster(**payload.model_dump())
    db.add(mode)
    await db.flush()
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=None,
        device_id=current.device_id,
        action="payment_mode.created",
        entity_type="payment_mode",
        entity_id=mode.id,
        new_value=payload.model_dump(mode="json"),
    )
    await db.commit()
    await db.refresh(mode)
    return mode


@router.put("/payment-modes/{mode_id}", response_model=PaymentModeOut)
async def update_payment_mode(
    mode_id: uuid.UUID,
    payload: PaymentModeIn,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("masterdata.payment_mode.update")),
) -> PaymentModeMaster:
    """Point 15 audit fix: is_active could only ever be set once, at creation
    — there was no way to deactivate an existing payment mode (e.g. retiring
    a discontinued wallet provider) without direct DB access."""
    mode = await db.get(PaymentModeMaster, mode_id)
    if mode is None:
        raise HTTPException(status_code=404, detail="Payment mode not found")
    if payload.code != mode.code:
        existing = (
            await db.execute(select(PaymentModeMaster).where(PaymentModeMaster.code == payload.code, PaymentModeMaster.id != mode_id))
        ).scalar_one_or_none()
        if existing is not None:
            raise HTTPException(status_code=409, detail=f"Payment mode code '{payload.code}' already exists")
    old_value = {"code": mode.code, "name": mode.name, "is_active": mode.is_active}
    for field, value in payload.model_dump().items():
        setattr(mode, field, value)
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=None,
        device_id=current.device_id,
        action="payment_mode.updated",
        entity_type="payment_mode",
        entity_id=mode.id,
        old_value=old_value,
        new_value=payload.model_dump(mode="json"),
    )
    await db.commit()
    await db.refresh(mode)
    return mode


@router.get("/reason-codes", response_model=list[ReasonCodeOut])
async def list_reason_codes(
    category: str | None = None,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("masterdata.reason_code.view")),
) -> list[ReasonCodeMaster]:
    stmt = select(ReasonCodeMaster).order_by(ReasonCodeMaster.category, ReasonCodeMaster.code)
    if category:
        stmt = stmt.where(ReasonCodeMaster.category == category)
    return list((await db.execute(stmt)).scalars().all())


@router.post("/reason-codes", response_model=ReasonCodeOut, status_code=201)
async def create_reason_code(
    payload: ReasonCodeIn,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("masterdata.reason_code.create")),
) -> ReasonCodeMaster:
    existing = (
        await db.execute(select(ReasonCodeMaster).where(ReasonCodeMaster.category == payload.category, ReasonCodeMaster.code == payload.code))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"Reason code '{payload.code}' already exists in category '{payload.category}'")
    reason = ReasonCodeMaster(**payload.model_dump())
    db.add(reason)
    await db.flush()
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=None,
        device_id=current.device_id,
        action="reason_code.created",
        entity_type="reason_code",
        entity_id=reason.id,
        new_value=payload.model_dump(mode="json"),
    )
    await db.commit()
    await db.refresh(reason)
    return reason


@router.put("/reason-codes/{reason_id}", response_model=ReasonCodeOut)
async def update_reason_code(
    reason_id: uuid.UUID,
    payload: ReasonCodeIn,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("masterdata.reason_code.update")),
) -> ReasonCodeMaster:
    """Point 15 audit fix: this is the master table services/inventory.py
    reads to decide whether a stock adjustment needs approval (requires_approval)
    — it had no update path, so a reason code's approval requirement could
    never be corrected after creation, and it could never be deactivated."""
    reason = await db.get(ReasonCodeMaster, reason_id)
    if reason is None:
        raise HTTPException(status_code=404, detail="Reason code not found")
    if payload.category != reason.category or payload.code != reason.code:
        existing = (
            await db.execute(
                select(ReasonCodeMaster).where(
                    ReasonCodeMaster.category == payload.category,
                    ReasonCodeMaster.code == payload.code,
                    ReasonCodeMaster.id != reason_id,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise HTTPException(status_code=409, detail=f"Reason code '{payload.code}' already exists in category '{payload.category}'")
    old_value = {
        "category": reason.category,
        "code": reason.code,
        "description": reason.description,
        "requires_approval": reason.requires_approval,
        "is_active": reason.is_active,
    }
    for field, value in payload.model_dump().items():
        setattr(reason, field, value)
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=None,
        device_id=current.device_id,
        action="reason_code.updated",
        entity_type="reason_code",
        entity_id=reason.id,
        old_value=old_value,
        new_value=payload.model_dump(mode="json"),
    )
    await db.commit()
    await db.refresh(reason)
    return reason


@router.get("/chart-of-accounts", response_model=list[ChartOfAccountOut])
async def list_chart_of_accounts(
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("masterdata.chart_of_account.view")),
) -> list[ChartOfAccount]:
    return list((await db.execute(select(ChartOfAccount).order_by(ChartOfAccount.account_code))).scalars().all())


@router.post("/chart-of-accounts", response_model=ChartOfAccountOut, status_code=201)
async def create_chart_of_account(
    payload: ChartOfAccountIn,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("masterdata.chart_of_account.create")),
) -> ChartOfAccount:
    existing = (await db.execute(select(ChartOfAccount).where(ChartOfAccount.account_code == payload.account_code))).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"Account code '{payload.account_code}' already exists")
    account = ChartOfAccount(**payload.model_dump())
    db.add(account)
    await db.flush()
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=None,
        device_id=current.device_id,
        action="chart_of_account.created",
        entity_type="chart_of_account",
        entity_id=account.id,
        new_value=payload.model_dump(mode="json"),
    )
    await db.commit()
    await db.refresh(account)
    return account


@router.put("/chart-of-accounts/{account_id}", response_model=ChartOfAccountOut)
async def update_chart_of_account(
    account_id: uuid.UUID,
    payload: ChartOfAccountIn,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("masterdata.chart_of_account.update")),
) -> ChartOfAccount:
    account = await db.get(ChartOfAccount, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Chart of account entry not found")
    if payload.account_code != account.account_code:
        existing = (
            await db.execute(select(ChartOfAccount).where(ChartOfAccount.account_code == payload.account_code, ChartOfAccount.id != account_id))
        ).scalar_one_or_none()
        if existing is not None:
            raise HTTPException(status_code=409, detail=f"Account code '{payload.account_code}' already exists")
    old_value = {
        "account_code": account.account_code,
        "account_name": account.account_name,
        "account_type": account.account_type,
        "is_active": account.is_active,
    }
    for field, value in payload.model_dump().items():
        setattr(account, field, value)
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=None,
        device_id=current.device_id,
        action="chart_of_account.updated",
        entity_type="chart_of_account",
        entity_id=account.id,
        old_value=old_value,
        new_value=payload.model_dump(mode="json"),
    )
    await db.commit()
    await db.refresh(account)
    return account
