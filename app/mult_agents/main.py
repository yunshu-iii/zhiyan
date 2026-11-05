"""运行主入口：构建 Agent、初始化记忆与 checkpointer，并驱动工作流执行。"""

import argparse
import json
import importlib
import logging
import os
import sys
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Callable, Optional

from langchain_core.messages import BaseMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langchain.agents import create_agent

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "mult_agents"

from .config import AppConfig
from .graph import build_app as build_workflow_app
from .memory import MemoryManager
from .prompts import PROMPTS
from .state import ResearchState, create_initial_state
from .identity import checkpoint_thread_id
from .tools import (
    extract_requirements,
    outline_from_topics,
    dedupe_lines,
    web_search_stub,
    local_docs_lookup_stub,
    search_knowledge_base,
    init_rag_system,
    merge_notes,
    summarize_points,
    simple_calculator,
)
from .rag.core import RAGConfig
from .providers import create_chat_model
from .web_search import web_search_records


logger = logging.getLogger("mult_agents")

MEMORY_MANAGER: Optional[MemoryManager] = None
CHECKPOINTER_CONTEXT = None

ANSI = {
    "reset": "\033[0m",
    "cyan": "\033[36m",
    "magenta": "\033[35m",
    "yellow": "\033[33m",
    "green": "\033[32m",
    "red": "\033[31m",
}


def colorize(text: str, color: str) -> str:
    if os.getenv("NO_COLOR"):
        return text
    code = ANSI.get(color, "")
    if not code:
        return text
    return f"{code}{text}{ANSI['reset']}"


def emit(node: str, content: str):
    preview = content.replace("\n", " ")
    if len(preview) > 400:
        preview = preview[:400] + "..."
    logger.info("main.emit: event")


def collect_tool_calls(messages) -> tuple[list, list]:
    tools = []
    tool_outputs = []
    for msg in messages:
        tool_calls = getattr(msg, "tool_calls", None)
        if tool_calls:
            for call in tool_calls:
                name = call.get("name") if isinstance(call, dict) else None
                if name:
                    tools.append(name)
        name = getattr(msg, "name", None)
        msg_type = getattr(msg, "type", None)
        if msg_type == "tool" and name:
            tools.append(name)
            output = getattr(msg, "content", "")
            if output:
                tool_outputs.append(f"{name}: {output}")
    return tools, tool_outputs


def with_memory_context(state: ResearchState, user_prompt: str) -> str:
    memory_context = state.get("memory_context", "").strip()
    if not memory_context:
        return user_prompt
    return f"{user_prompt}\n\n[跨会话记忆]\n{memory_context}"


def log_inputs(node: str, agent_name: str, payload: dict):
    preview = {
        key: (value[:200] + "..." if isinstance(value, str) and len(value) > 200 else value)
        for key, value in payload.items()
    }
    logger.info("main.log_inputs: event")


def plan_node(state: ResearchState, agent, agent_name: str) -> ResearchState:
    logger.info("main.plan_node: event")
    log_inputs("plan", agent_name, {"query": state["query"]})
    human = HumanMessage(content=with_memory_context(state, f"用户需求：{state['query']}"))
    result = agent.invoke({"messages": state["messages"] + [human]})
    last_message = result["messages"][-1]
    tools, tool_outputs = collect_tool_calls(result["messages"])
    logger.info("main.plan_node: event")
    for item in tool_outputs[:5]:
        logger.info("main.plan_node: event")
    logger.info("main.plan_node: event")
    emit("plan", last_message.content)
    return {
        "plan": last_message.content,
        "messages": [human, last_message],
    }


def web_search_node(state: ResearchState, agent, agent_name: str) -> ResearchState:
    logger.info("main.web_search_node: event")
    log_inputs("web_search", agent_name, {"plan": state["plan"], "query": state["query"]})
    human = HumanMessage(content=with_memory_context(state, f"计划：{state['plan']}\n问题：{state['query']}"))
    result = agent.invoke({"messages": state["messages"] + [human]})
    last_message = result["messages"][-1]
    tools, tool_outputs = collect_tool_calls(result["messages"])
    logger.info("main.web_search_node: event")
    for item in tool_outputs[:5]:
        logger.info("main.web_search_node: event")
    logger.info("main.web_search_node: event")
    emit("web_search", last_message.content)
    return {
        "web_search": last_message.content,
        "messages": [human, last_message],
    }


def local_rag_node(state: ResearchState, agent, agent_name: str) -> ResearchState:
    logger.info("main.local_rag_node: event")
    log_inputs("local_rag", agent_name, {"plan": state["plan"], "query": state["query"]})
    human = HumanMessage(content=with_memory_context(state, f"计划：{state['plan']}\n问题：{state['query']}"))
    result = agent.invoke({"messages": state["messages"] + [human]})
    last_message = result["messages"][-1]
    tools, tool_outputs = collect_tool_calls(result["messages"])
    logger.info("main.local_rag_node: event")
    for item in tool_outputs[:5]:
        logger.info("main.local_rag_node: event")
    logger.info("main.local_rag_node: event")
    emit("local_rag", last_message.content)
    return {
        "local_rag": last_message.content,
        "messages": [human, last_message],
    }


def deep_dive_node(state: ResearchState, agent, agent_name: str) -> ResearchState:
    logger.info("main.deep_dive_node: event")
    if not state["web_search"] or not state["local_rag"]:
        logger.info("main.deep_dive_node: event")
        return {}
    log_inputs("deep_dive", agent_name, {"query": state["query"], "web_search": state["web_search"], "local_rag": state["local_rag"]})
    human = HumanMessage(
        content=(
            with_memory_context(state, f"问题：{state['query']}") + "\n"
            f"网络资料：{state['web_search']}\n"
            f"本地资料：{state['local_rag']}"
        )
    )
    result = agent.invoke({"messages": state["messages"] + [human]})
    last_message = result["messages"][-1]
    tools, tool_outputs = collect_tool_calls(result["messages"])
    logger.info("main.deep_dive_node: event")
    for item in tool_outputs[:5]:
        logger.info("main.deep_dive_node: event")
    logger.info("main.deep_dive_node: event")
    emit("deep_dive", last_message.content)
    return {
        "deep_dive": last_message.content,
        "messages": [human, last_message],
    }


def analyze_node(state: ResearchState, agent, agent_name: str) -> ResearchState:
    logger.info("main.analyze_node: event")
    log_inputs("analyze", agent_name, {"query": state["query"], "plan": state["plan"], "deep_dive": state["deep_dive"]})
    human = HumanMessage(
        content=(
            with_memory_context(state, f"问题：{state['query']}") + "\n"
            f"计划：{state['plan']}\n"
            f"深度结论：{state['deep_dive']}"
        )
    )
    result = agent.invoke({"messages": state["messages"] + [human]})
    last_message = result["messages"][-1]
    tools, tool_outputs = collect_tool_calls(result["messages"])
    logger.info("main.analyze_node: event")
    for item in tool_outputs[:5]:
        logger.info("main.analyze_node: event")
    logger.info("main.analyze_node: event")
    emit("analyze", last_message.content)
    return {
        "analysis": last_message.content,
        "messages": [human, last_message],
    }


def codegen_node(state: ResearchState, agent, agent_name: str) -> ResearchState:
    logger.info("main.codegen_node: event")
    log_inputs("codegen", agent_name, {"query": state["query"], "analysis": state["analysis"]})
    human = HumanMessage(
        content=(
            with_memory_context(state, f"问题：{state['query']}") + "\n"
            f"分析：{state['analysis']}\n"
            f"请给出可执行的方案和代码片段。"
        )
    )
    result = agent.invoke({"messages": state["messages"] + [human]})
    last_message = result["messages"][-1]
    tools, tool_outputs = collect_tool_calls(result["messages"])
    logger.info("main.codegen_node: event")
    for item in tool_outputs[:5]:
        logger.info("main.codegen_node: event")
    logger.info("main.codegen_node: event")
    emit("codegen", last_message.content)
    return {
        "code": last_message.content,
        "messages": [human, last_message],
    }


def write_node(state: ResearchState, agent, agent_name: str) -> ResearchState:
    logger.info("main.write_node: event")
    log_inputs("write", agent_name, {"query": state["query"], "plan": state["plan"], "analysis": state["analysis"], "code": state["code"]})
    human = HumanMessage(
        content=(
            with_memory_context(state, f"问题：{state['query']}") + "\n"
            f"计划：{state['plan']}\n"
            f"分析：{state['analysis']}\n"
            f"方案与代码：{state['code']}\n"
            f"请输出最终答案。"
        )
    )
    result = agent.invoke({"messages": state["messages"] + [human]})
    last_message = result["messages"][-1]
    tools, tool_outputs = collect_tool_calls(result["messages"])
    logger.info("main.write_node: event")
    for item in tool_outputs[:5]:
        logger.info("main.write_node: event")
    logger.info("main.write_node: event")
    emit("write", last_message.content)
    return {
        "draft": last_message.content,
        "final": last_message.content,
        "messages": [human, last_message],
    }


def build_memory_manager(config: AppConfig) -> Optional[MemoryManager]:
    if not config.enable_memory:
        return None
    try:
        return MemoryManager(
            short_term_ttl=config.short_term_ttl_seconds,
            short_term_max_messages=config.short_term_max_messages,
            short_term_summary_threshold=config.short_term_summary_threshold,
            tenant_id=config.tenant_id,
            short_term_backend=config.short_term_backend,
            long_term_backend=config.long_term_backend,
            long_term_scope=config.long_term_scope,
            save_conversation_task=config.save_conversation_task,
            enable_milvus=config.enable_milvus,
            redis_url=config.redis_url,
            postgres_dsn=config.postgres_dsn,
            milvus_host=config.milvus_host,
            milvus_port=config.milvus_port,
            milvus_collection=config.milvus_collection,
            embedding_api_key=config.embedding_api_key,
            summary_api_key=config.api_key,
            summary_model=config.model,
            deepseek_base_url=config.deepseek_base_url,
        )
    except Exception as exc:
        logger.warning("main.build_memory_manager: event")
        return None


def build_checkpointer(config: AppConfig):
    global CHECKPOINTER_CONTEXT
    backend = config.checkpointer_backend
    if backend in {"postgres", "auto"} and config.enable_memory and config.postgres_dsn:
        postgres_saver = None
        postgres_import_error = ""
        try:
            module = importlib.import_module("langgraph.checkpoint.postgres")
            postgres_saver = getattr(module, "PostgresSaver", None)
        except Exception as exc:
            postgres_import_error = str(exc)
        if postgres_saver is None:
            try:
                module = importlib.import_module("langgraph_checkpoint_postgres")
                postgres_saver = getattr(module, "PostgresSaver", None)
            except Exception as exc:
                postgres_import_error = postgres_import_error or str(exc)
        if postgres_saver is None:
            message = (
                "PostgreSQL checkpointer 模块不可用。请安装: pip install langgraph-checkpoint-postgres "
                f"| import_error={postgres_import_error or 'unknown'}"
            )
            if backend == "postgres":
                logger.warning("main.build_checkpointer: event")
            else:
                logger.info("main.build_checkpointer: event")
        else:
            try:
                CHECKPOINTER_CONTEXT = postgres_saver.from_conn_string(config.postgres_dsn)
                checkpointer = CHECKPOINTER_CONTEXT.__enter__()
                checkpointer.setup()
                logger.info("main.build_checkpointer: event")
                return checkpointer
            except Exception as exc:
                logger.warning("main.build_checkpointer: event")
    if backend in {"redis", "auto"} and config.enable_memory and config.redis_url:
        from langgraph.checkpoint.redis import RedisSaver

        candidate_urls = [config.redis_url]
        if "redis://root:" in config.redis_url:
            candidate_urls.append(config.redis_url.replace("redis://root:", "redis://:"))
        last_exc = None
        for url in candidate_urls:
            try:
                CHECKPOINTER_CONTEXT = RedisSaver.from_conn_string(url)
                checkpointer = CHECKPOINTER_CONTEXT.__enter__()
                checkpointer.setup()
                logger.info("main.build_checkpointer: event")
                return checkpointer
            except Exception as exc:
                last_exc = exc
        if last_exc and "FT._LIST" in str(last_exc):
            logger.warning("main.build_checkpointer: event")
        else:
            logger.warning("main.build_checkpointer: event")
    if backend == "memory":
        logger.info("main.build_checkpointer: event")
    return InMemorySaver()


def parse_cli_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="multi-agent memory runner")
    parser.add_argument("--config", type=str, default=None)
    parser.add_argument("--tenant-id", type=str, default=None)
    parser.add_argument("--user-id", type=str, default=None)
    parser.add_argument("--thread-id", type=str, default=None)
    parser.add_argument("--short-term-backend", choices=["postgres", "redis", "memory"], default=None)
    parser.add_argument("--long-term-backend", choices=["postgres", "sqlite", "disabled"], default=None)
    parser.add_argument("--long-term-scope", choices=["user", "thread"], default=None)
    parser.add_argument("--save-conversation-task", choices=["true", "false"], default=None)
    parser.add_argument("--checkpointer-backend", choices=["postgres", "redis", "memory", "auto"], default=None)
    parser.add_argument("--enable-memory", choices=["true", "false"], default=None)
    parser.add_argument("--enable-milvus", choices=["true", "false"], default=None)
    parser.add_argument("--memory-top-k", type=int, default=None)
    parser.add_argument("--once-query", type=str, default=None)
    return parser.parse_args()


def build_runtime_config(args: argparse.Namespace) -> AppConfig:
    config = AppConfig.from_file(args.config) if args.config else AppConfig.from_file()
    overrides = {
        "tenant_id": args.tenant_id,
        "user_id": args.user_id,
        "thread_id": args.thread_id,
        "short_term_backend": args.short_term_backend,
        "long_term_backend": args.long_term_backend,
        "long_term_scope": args.long_term_scope,
        "checkpointer_backend": args.checkpointer_backend,
        "memory_top_k": args.memory_top_k,
    }
    if args.enable_memory is not None:
        overrides["enable_memory"] = args.enable_memory == "true"
    if args.enable_milvus is not None:
        overrides["enable_milvus"] = args.enable_milvus == "true"
    if args.save_conversation_task is not None:
        overrides["save_conversation_task"] = args.save_conversation_task == "true"
    config = config.with_overrides(**overrides)
    logger.info("main.build_runtime_config: event")
    return config


@dataclass(frozen=True)
class AgentBundle:
    intent_router: any
    planner: any
    scout_web: any
    scout_local: any
    evidence_judge: any
    analyst: any
    direct_responder: any
    writer: any
    search_records: Callable = field(default=web_search_records, repr=False)


def build_agent(model: str, api_key: str, prompt_key: str, temperature: float, tools: list, base_url: str | None = None):
    llm = create_chat_model(api_key, model, temperature, base_url)
    prompt = PROMPTS[prompt_key]
    return create_agent(model=llm, tools=tools, system_prompt=prompt)


def build_agents(model: str, api_key: str, config: AppConfig) -> AgentBundle:
    if config.enable_milvus and config.knowledge_collection == config.milvus_collection:
        raise ValueError("知识库集合不能与个人记忆集合相同")
    rag_config = RAGConfig(
        milvus_host=config.milvus_host,
        milvus_port=config.milvus_port,
        collection_name=config.knowledge_collection,
    )
    if config.enable_milvus:
        init_rag_system(api_key=config.embedding_api_key, config=rag_config)
    # 工具由工作流节点调用，各角色 Agent 专注于结构化分析。
    return AgentBundle(
        intent_router=build_agent(model, api_key, "intent_router", 0.0, [], config.deepseek_base_url),
        planner=build_agent(model, api_key, "plan", 0.3, [], config.deepseek_base_url),
        scout_web=build_agent(model, api_key, "web_search", 0.4, [], config.deepseek_base_url),
        scout_local=build_agent(model, api_key, "local_rag", 0.4, [], config.deepseek_base_url),
        evidence_judge=build_agent(model, api_key, "deep_dive", 0.2, [], config.deepseek_base_url),
        analyst=build_agent(model, api_key, "analyze", 0.3, [], config.deepseek_base_url),
        direct_responder=build_agent(model, api_key, "direct_answer", 0.2, [], config.deepseek_base_url),
        writer=build_agent(model, api_key, "write", 0.4, [], config.deepseek_base_url),
        search_records=partial(web_search_records, api_key=api_key, base_url=config.deepseek_base_url),
    )


def run_query(app, config: AppConfig, query: str):
    memory_context = ""
    if MEMORY_MANAGER:
        try:
            memory_context = MEMORY_MANAGER.build_personalized_prompt_context(
                user_id=config.user_id,
                thread_id=config.thread_id,
                query=query,
                tenant_id=config.tenant_id,
                max_memories=config.memory_top_k,
            )
        except Exception as exc:
            logger.warning("main.run_query: event")
    state = create_initial_state(
        query=query,
        max_iterations=config.max_iterations,
        user_id=config.user_id,
        tenant_id=config.tenant_id,
        memory_context=memory_context,
    )
    result = app.invoke(
        state,
        {"configurable": {"thread_id": checkpoint_thread_id(config.tenant_id, config.user_id, config.thread_id)}},
    )
    final = result["final"]
    if MEMORY_MANAGER:
        try:
            MEMORY_MANAGER.persist_turn(
                tenant_id=config.tenant_id,
                user_id=config.user_id,
                thread_id=config.thread_id,
                query=query,
                answer=final,
            )
        except Exception as exc:
            logger.warning("main.run_query: event")
    return final


def read_user_input(prompt: str = "你: ") -> str:
    try:
        return input(prompt)
    except UnicodeDecodeError:
        print(prompt, end="", flush=True)
        raw = sys.stdin.buffer.readline()
        if raw == b"":
            raise EOFError
        encoding = sys.stdin.encoding or "utf-8"
        recovered = raw.decode(encoding, errors="replace").rstrip("\r\n")
        logger.warning("main.read_user_input: event")
        return recovered


def main():
    global MEMORY_MANAGER
    global CHECKPOINTER_CONTEXT
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    args = parse_cli_args()
    config = build_runtime_config(args)
    MEMORY_MANAGER = build_memory_manager(config)
    agents = build_agents(config.model, config.api_key, config)
    checkpointer = build_checkpointer(config)
    app = build_workflow_app(agents, checkpointer)
    if args.once_query:
        response = run_query(app, config, args.once_query)
        print(f"\nAI: {response}\n")
    else:
        while True:
            try:
                query = read_user_input("你: ").strip()
            except EOFError:
                break
            if not query:
                continue
            if query.lower() in {"quit", "exit", "退出"}:
                break
            if query.lower() in {"/memory", "memory-status"} and MEMORY_MANAGER:
                print(json.dumps(MEMORY_MANAGER.get_memory_stats(config.user_id), ensure_ascii=False, indent=2))
                continue
            if query.lower() in {"/memory-trace", "memory-trace"} and MEMORY_MANAGER:
                print(json.dumps(MEMORY_MANAGER.get_last_trace(), ensure_ascii=False, indent=2))
                continue
            response = run_query(app, config, query)
            print(f"\nAI: {response}\n")
    if CHECKPOINTER_CONTEXT:
        CHECKPOINTER_CONTEXT.__exit__(None, None, None)
        CHECKPOINTER_CONTEXT = None


if __name__ == "__main__":
    main()
