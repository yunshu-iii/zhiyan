from pydantic import BaseModel, Field


class ResearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=16000)
    user_id: str = Field(default="default_user", min_length=1, max_length=128)
    thread_id: str = Field(default="default_thread", min_length=1, max_length=128)
    tenant_id: str = Field(default="default_tenant", min_length=1, max_length=128)
    max_iterations: int | None = Field(default=None, ge=1, le=6)
    enable_memory: bool | None = None


class ResearchResponse(BaseModel):
    query: str
    user_id: str
    thread_id: str
    tenant_id: str
    final: str
