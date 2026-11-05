"""Local conversation management; all queries are scoped by tenant and user."""
from fastapi import APIRouter, Depends, HTTPException, Query, Path
from pydantic import BaseModel, Field

from backend.service import get_history_store
from backend.service.history_store import HistoryStore

router = APIRouter(prefix='/api/v1/conversations', tags=['conversations'])


class ConversationScope(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    user_id: str = Field(min_length=1, max_length=128)


@router.get('')
def list_conversations(tenant_id: str = Query(min_length=1, max_length=128),
                       user_id: str = Query(min_length=1, max_length=128),
                       limit: int = Query(default=50, ge=1, le=100),
                       offset: int = Query(default=0, ge=0),
                       store: HistoryStore = Depends(get_history_store)):
    return store.list_conversations(tenant_id,user_id,limit,offset)


@router.post('', status_code=201)
def create_conversation(scope: ConversationScope, store: HistoryStore = Depends(get_history_store)):
    return store.create_conversation(scope.tenant_id,scope.user_id)


@router.get('/{thread_id}')
def get_conversation(thread_id: str = Path(min_length=1, max_length=128), tenant_id: str = Query(min_length=1, max_length=128),
                     user_id: str = Query(min_length=1, max_length=128),
                     store: HistoryStore = Depends(get_history_store)):
    detail = store.get_conversation(tenant_id,user_id,thread_id)
    if detail is None:
        raise HTTPException(status_code=404, detail='会话不存在')
    return detail
