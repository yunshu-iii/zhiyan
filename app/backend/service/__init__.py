from functools import lru_cache

from backend.config import AppSettings
from .workflow_service import WorkflowService
from .history_store import HistoryStore
from mult_agents.config import AppConfig


@lru_cache(maxsize=1)
def get_history_store() -> HistoryStore:
    config = AppConfig.from_file(AppSettings().config_path, require_llm=False)
    return HistoryStore(config.postgres_dsn)


@lru_cache(maxsize=1)
def get_workflow_service() -> WorkflowService:
    settings = AppSettings()
    return WorkflowService(config_path=settings.config_path, history_store=get_history_store())


__all__ = ["WorkflowService", "get_workflow_service", "get_history_store"]
