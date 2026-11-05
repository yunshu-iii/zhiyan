import asyncio
import logging
from threading import Lock, Thread, BoundedSemaphore
from typing import AsyncIterator, Callable

from mult_agents.config import AppConfig
from mult_agents.graph import build_app as build_workflow_app
from mult_agents.main import build_agents, build_checkpointer, build_memory_manager
from mult_agents.state import create_initial_state
from mult_agents.identity import checkpoint_thread_id
from .history_store import HistoryStore

logger = logging.getLogger('backend.workflow')


class ResearchBusyError(RuntimeError):
    """The per-process research worker limit has been reached."""


class WorkflowService:
    def __init__(self, config_path: str, history_store: HistoryStore | None = None):
        self._config_path = config_path
        self._lock = Lock()
        self._initialized = False
        self._base_config: AppConfig | None = None
        self._memory_manager = None
        self._app = None
        self._history = history_store
        self._research_slots = BoundedSemaphore(2)

    def _record_and_execute(self, execute, query, user_id, thread_id, tenant_id,
                      max_iterations, enable_memory, **kwargs):
        if self._history is None:
            config = AppConfig.from_file(self._config_path, require_llm=False)
            self._history = HistoryStore(config.postgres_dsn)
        scope = dict(tenant_id=tenant_id, user_id=user_id, thread_id=thread_id)
        self._history.append_message(**scope, role='user', content=query)
        try:
            result = execute(query=query, user_id=user_id, thread_id=thread_id, tenant_id=tenant_id,
                             max_iterations=max_iterations, enable_memory=enable_memory, **kwargs)
        except Exception:
            self._history.append_message(**scope, role='assistant', content='请求失败，请重试。')
            raise
        self._history.append_message(**scope, role='assistant', content=result[0])
        return result

    def _run_recorded(self, execute, query, user_id, thread_id, tenant_id,
                      max_iterations, enable_memory, **kwargs):
        if not self._research_slots.acquire(blocking=False):
            raise ResearchBusyError('研究任务繁忙，请稍后重试。')
        try:
            return self._record_and_execute(execute, query, user_id, thread_id, tenant_id,
                                            max_iterations, enable_memory, **kwargs)
        finally:
            self._research_slots.release()

    def _run_sync(self, query, user_id, thread_id, tenant_id, max_iterations, enable_memory):
        return self._run_recorded(self._execute_sync, query, user_id, thread_id, tenant_id,
                                  max_iterations, enable_memory)

    def _run_sync_with_events(self, query, user_id, thread_id, tenant_id, max_iterations, enable_memory, emit):
        return self._run_recorded(self._execute_sync_with_events, query, user_id, thread_id, tenant_id,
                                  max_iterations, enable_memory, emit=emit)

    def _persist_model_memory(self, config, query, answer):
        if self._memory_manager and config.enable_memory:
            try:
                self._memory_manager.persist_turn(
                    tenant_id=config.tenant_id, user_id=config.user_id,
                    thread_id=config.thread_id, query=query, answer=answer,
                )
            except Exception as exc:
                # Model-memory compression must not discard a successfully generated answer.
                logger.warning("workflow_service._persist_model_memory: event")

    def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        with self._lock:
            if self._initialized:
                return
            base_config = AppConfig.from_file(self._config_path)
            self._memory_manager = build_memory_manager(base_config)
            agents = build_agents(base_config.model, base_config.api_key, base_config)
            checkpointer = build_checkpointer(base_config)
            self._app = build_workflow_app(agents, checkpointer)
            self._base_config = base_config
            self._initialized = True

    def _build_runtime_config(
        self,
        user_id: str,
        thread_id: str,
        tenant_id: str,
        max_iterations: int | None,
        enable_memory: bool | None,
    ) -> AppConfig:
        if self._base_config is None:
            raise RuntimeError("service not initialized")
        overrides = {
            "user_id": user_id,
            "thread_id": thread_id,
            "tenant_id": tenant_id,
            "max_iterations": max_iterations if max_iterations is not None else self._base_config.max_iterations,
        }
        if enable_memory is not None:
            overrides["enable_memory"] = enable_memory
        return self._base_config.with_overrides(**overrides)

    def _execute_sync(
        self,
        query: str,
        user_id: str,
        thread_id: str,
        tenant_id: str,
        max_iterations: int | None,
        enable_memory: bool | None,
    ) -> tuple[str, str]:
        self._ensure_initialized()
        runtime_config = self._build_runtime_config(
            user_id=user_id,
            thread_id=thread_id,
            tenant_id=tenant_id,
            max_iterations=max_iterations,
            enable_memory=enable_memory,
        )
        memory_context = ""
        if self._memory_manager and runtime_config.enable_memory:
            memory_context = self._memory_manager.build_personalized_prompt_context(
                user_id=runtime_config.user_id,
                thread_id=runtime_config.thread_id,
                query=query,
                tenant_id=runtime_config.tenant_id,
                max_memories=runtime_config.memory_top_k,
            )
        state = create_initial_state(
            query=query,
            max_iterations=runtime_config.max_iterations,
            user_id=runtime_config.user_id,
            tenant_id=runtime_config.tenant_id,
            memory_context=memory_context,
        )
        result = self._app.invoke(
            state,
            {"configurable": {"thread_id": checkpoint_thread_id(runtime_config.tenant_id, runtime_config.user_id, runtime_config.thread_id)}},
        )
        final = result.get("final", "")
        route = str(result.get("intent", "multiagent"))
        self._persist_model_memory(runtime_config, query, final)
        return final, route

    @staticmethod
    def _node_message(node_name: str) -> str:
        mapping = {
            "intent": "Intent Router 正在识别问题意图",
            "direct_answer": "Direct Responder 正在快速作答",
            "plan": "Planner 正在拆解问题",
            "web_search": "Web Scout 正在检索网络证据",
            "local_rag": "Local Scout 正在检索本地知识库",
            "deep_dive": "Evidence Judge 正在进行证据裁判",
            "analyze": "Analyst 正在生成结论",
            "reflect": "Reflect 正在生成补搜计划",
            "write": "Writer 正在撰写最终报告",
        }
        return mapping.get(node_name, f"{node_name} 正在执行")

    def _execute_sync_with_events(
        self,
        query: str,
        user_id: str,
        thread_id: str,
        tenant_id: str,
        max_iterations: int | None,
        enable_memory: bool | None,
        emit: Callable[[dict], None],
    ) -> tuple[str, str]:
        self._ensure_initialized()
        runtime_config = self._build_runtime_config(
            user_id=user_id,
            thread_id=thread_id,
            tenant_id=tenant_id,
            max_iterations=max_iterations,
            enable_memory=enable_memory,
        )
        memory_context = ""
        if self._memory_manager and runtime_config.enable_memory:
            memory_context = self._memory_manager.build_personalized_prompt_context(
                user_id=runtime_config.user_id,
                thread_id=runtime_config.thread_id,
                query=query,
                tenant_id=runtime_config.tenant_id,
                max_memories=runtime_config.memory_top_k,
            )
        state = create_initial_state(
            query=query,
            max_iterations=runtime_config.max_iterations,
            user_id=runtime_config.user_id,
            tenant_id=runtime_config.tenant_id,
            memory_context=memory_context,
        )
        final = ""
        route = "multiagent"
        config = {"configurable": {"thread_id": checkpoint_thread_id(runtime_config.tenant_id, runtime_config.user_id, runtime_config.thread_id)}}
        for update in self._app.stream(state, config, stream_mode="updates"):
            if not isinstance(update, dict):
                continue
            for node_name, node_output in update.items():
                emit({"type": "phase", "node": node_name, "message": self._node_message(str(node_name))})
                if isinstance(node_output, dict):
                    if node_name == "intent":
                        detected = str(node_output.get("intent", route)).strip().lower()
                        if detected in {"direct", "multiagent"}:
                            route = detected
                    value = node_output.get("final")
                    if value:
                        final = str(value)
        if not final:
            result = self._app.invoke(state, config)
            final = str(result.get("final", ""))
            route = str(result.get("intent", route)).strip().lower()
        self._persist_model_memory(runtime_config, query, final)
        return final, route

    async def run(
        self,
        query: str,
        user_id: str,
        thread_id: str,
        tenant_id: str,
        max_iterations: int | None,
        enable_memory: bool | None,
    ) -> str:
        final, _ = await asyncio.to_thread(
            self._run_sync,
            query,
            user_id,
            thread_id,
            tenant_id,
            max_iterations,
            enable_memory,
        )
        return final

    async def run_with_route(
        self,
        query: str,
        user_id: str,
        thread_id: str,
        tenant_id: str,
        max_iterations: int | None,
        enable_memory: bool | None,
    ) -> tuple[str, str]:
        return await asyncio.to_thread(
            self._run_sync,
            query,
            user_id,
            thread_id,
            tenant_id,
            max_iterations,
            enable_memory,
        )

    async def stream_events(
        self,
        query: str,
        user_id: str,
        thread_id: str,
        tenant_id: str,
        max_iterations: int | None,
        enable_memory: bool | None,
    ) -> AsyncIterator[dict]:
        queue: asyncio.Queue[dict] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def emit(event: dict) -> None:
            asyncio.run_coroutine_threadsafe(queue.put(event), loop)

        def worker() -> None:
            try:
                final, route = self._run_sync_with_events(
                    query=query,
                    user_id=user_id,
                    thread_id=thread_id,
                    tenant_id=tenant_id,
                    max_iterations=max_iterations,
                    enable_memory=enable_memory,
                    emit=emit,
                )
                emit({"type": "route", "message": "已走直接回答路径" if route == "direct" else "已走多智能体研究路径"})
                emit(
                    {
                        "type": "final",
                        "query": query,
                        "user_id": user_id,
                        "thread_id": thread_id,
                        "tenant_id": tenant_id,
                        "final": final,
                    }
                )
            except Exception as exc:
                emit({"type": "error", "message": "研究任务繁忙，请稍后重试。" if isinstance(exc, ResearchBusyError) else "研究任务执行失败，请检查服务配置或稍后重试。"})
            finally:
                emit({"type": "__done__"})

        Thread(target=worker, daemon=True).start()
        while True:
            event = await queue.get()
            if event.get("type") == "__done__":
                break
            yield event
