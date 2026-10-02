import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission, require_store_access
from app.core.database import get_db
from app.schemas.schemas import AiInsightOut
from app.services.ai import get_insight
from app.services.ai_chat import MAX_HISTORY, MAX_MESSAGE_CHARS, answer

router = APIRouter(prefix="/ai", tags=["ai"])


class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=4000)


class ChatIn(BaseModel):
    store_id: uuid.UUID
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    history: list[ChatTurn] = Field(default_factory=list, max_length=MAX_HISTORY)


class ChatOut(BaseModel):
    answer: str
    # False when no AI provider is configured (or the call failed) and the
    # answer is the plain-text digest of the data instead.
    llm_used: bool
    # ERP modules whose data was available to this answer for this user.
    sources: list[str]
    generated_at: datetime


@router.get("/insights", response_model=AiInsightOut)
async def insights(
    store_id: uuid.UUID,
    question: str | None = None,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("ai.insight.view")),
) -> dict:
    require_store_access(store_id, current)
    return await get_insight(db, store_id=store_id, question=question)


@router.post("/chat", response_model=ChatOut)
async def chat(
    payload: ChatIn,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("ai.insight.view")),
) -> dict:
    """ERP assistant. Context is built server-side from the caller's own
    permissions and store scope — the client only sends the question and the
    recent conversation, never data."""
    require_store_access(payload.store_id, current)
    return await answer(
        db,
        current=current,
        store_id=payload.store_id,
        message=payload.message,
        history=[t.model_dump() for t in payload.history],
    )
