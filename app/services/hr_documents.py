"""Document checklist for joining/exit (Point 12 audit fix) — previously
there was no concept of required onboarding/offboarding documents at all.
Checklist items are master data; per-employee rows are auto-seeded so HR
always sees a concrete pending/submitted/verified list, not a blank slate."""

import uuid
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser
from app.models.models_phase4 import DocumentChecklistItem, EmployeeDocument
from app.services.audit import write_audit


async def seed_employee_documents(db: AsyncSession, *, employee_id: uuid.UUID, applies_to: str) -> None:
    items = (
        await db.execute(
            select(DocumentChecklistItem).where(
                DocumentChecklistItem.applies_to == applies_to,
                DocumentChecklistItem.is_active.is_(True),
            )
        )
    ).scalars().all()
    if not items:
        return

    existing_ids = set(
        (
            await db.execute(
                select(EmployeeDocument.checklist_item_id).where(
                    EmployeeDocument.employee_id == employee_id,
                    EmployeeDocument.checklist_item_id.in_([i.id for i in items]),
                )
            )
        ).scalars().all()
    )
    for item in items:
        if item.id in existing_ids:
            continue
        db.add(EmployeeDocument(employee_id=employee_id, checklist_item_id=item.id, status="pending"))
    await db.flush()


async def update_employee_document(db: AsyncSession, *, current: CurrentUser, doc_id: uuid.UUID, status: str, reference: str | None, notes: str | None) -> EmployeeDocument:
    if status not in ("pending", "submitted", "verified", "waived"):
        raise HTTPException(status_code=400, detail="Invalid status")

    doc = await db.get(EmployeeDocument, doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Employee document not found")

    doc.status = status
    doc.reference = reference
    doc.notes = notes
    doc.updated_by = current.user_id
    doc.updated_at = datetime.now(timezone.utc)

    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=None,
        device_id=current.device_id,
        action="employee_document.updated",
        entity_type="employee_document",
        entity_id=doc.id,
        new_value={"status": status},
        reason=None,
    )
    return doc
