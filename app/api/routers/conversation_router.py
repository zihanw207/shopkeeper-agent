"""本地演示用会话 API；部署给多用户前需增加认证和会话所有权校验。"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from app.repositories.conversation_store import (
    ConversationNotFound,
    ConversationStore,
    get_conversation_store,
)

conversation_router = APIRouter(prefix="/api/conversations", tags=["conversations"])
Store = Annotated[ConversationStore, Depends(get_conversation_store)]


@conversation_router.post("", status_code=201)
async def create_conversation(store: Store):
    return await store.create()


@conversation_router.get("")
async def list_conversations(store: Store):
    return await store.list()


@conversation_router.get("/{conversation_id}")
async def get_conversation(conversation_id: UUID, store: Store):
    try:
        return await store.get(str(conversation_id))
    except ConversationNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
