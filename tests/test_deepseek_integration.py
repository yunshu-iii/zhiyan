"""Offline provider contracts against a local HTTP server; no paid API traffic."""
import json
import contextlib
import io
import gc
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
from mult_agents.config import AppConfig
from mult_agents import tools


class ProviderContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                self.server.calls.append({"path": self.path, "body": body, "auth": self.headers.get("Authorization")})
                self.send_response(self.server.response_status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                response = self.server.response_body
                if self.path.endswith("/chat/completions"):
                    response = {"id": "offline-chat", "object": "chat.completion", "created": 0,
                                "model": "deepseek-flash", "choices": [{"index": 0, "finish_reason": "stop",
                                "message": {"role": "assistant", "content": "offline answer"}}],
                                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
                self.wfile.write(json.dumps(response).encode())

            def log_message(self, *args):
                pass

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.server.calls = []
        self.server.response_status = 200
        self.server.response_body = {"results": []}
        self.environment = patch.dict(os.environ, {
            "DEEPSEEK_API_KEY": "deepseek-offline-secret",
            "DASHSCOPE_API_KEY": "dashscope-offline-secret",
            "DEEPSEEK_BASE_URL": self.base + "/v1",
            "MODEL": "",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_config_keeps_model_and_embedding_credentials_separate(self):
        config = AppConfig.from_env()
        self.assertEqual(config.api_key, "deepseek-offline-secret")
        self.assertEqual(config.embedding_api_key, "dashscope-offline-secret")
        self.assertEqual(config.model, "deepseek-flash")

    def test_embedding_only_config_does_not_require_deepseek(self):
        os.environ.pop("DEEPSEEK_API_KEY")
        with self.assertRaisesRegex(ValueError, "DEEPSEEK_API_KEY"):
            AppConfig.from_env()
        config = AppConfig.from_env(require_llm=False)
        self.assertEqual(config.embedding_api_key, "dashscope-offline-secret")
        self.assertEqual(config.api_key, "")

    def test_chat_uses_deepseek_and_leaves_embedding_key_untouched(self):
        from mult_agents.providers import create_chat_model
        model = create_chat_model("deepseek-offline-secret", "deepseek-flash", 0.2, self.base + "/v1")
        result = model.invoke("offline probe")
        self.assertEqual(result.content, "offline answer")
        call = self.server.calls[-1]
        self.assertEqual(call["path"], "/v1/chat/completions")
        self.assertEqual(call["auth"], "Bearer deepseek-offline-secret")
        self.assertEqual(call["body"]["thinking"], {"type": "disabled"})
        self.assertEqual(os.environ["DASHSCOPE_API_KEY"], "dashscope-offline-secret")

    def test_langgraph_agent_can_run_with_the_deepseek_chat_adapter(self):
        from mult_agents.main import build_agent
        agent = build_agent("deepseek-flash", "deepseek-offline-secret", "direct_answer", 0.2, [], self.base + "/v1")
        result = agent.invoke({"messages": [{"role": "user", "content": "offline graph probe"}]})
        self.assertEqual(result["messages"][-1].content, "offline answer")
        self.assertEqual(self.server.calls[-1]["auth"], "Bearer deepseek-offline-secret")

    def test_ingestion_uses_only_the_embedding_credential(self):
        from mult_agents.rag import ingest, core
        from mult_agents import config as config_module
        config = AppConfig.from_env()
        with tempfile.TemporaryDirectory() as temporary:
            document = Path(temporary) / "notes.md"
            document.write_text("offline document", encoding="utf-8")
            with patch.object(config_module.AppConfig, "from_file", return_value=config) as loader, \
                 patch.object(core, "RAGSystem") as rag, \
                 patch.object(sys, "argv", [str(ingest.__file__), str(document)]), \
                 contextlib.redirect_stdout(io.StringIO()):
                rag.return_value.ingest_paths.return_value = 1
                ingest.main()
            loader.assert_called_once_with(require_llm=False)
            self.assertEqual(rag.call_args.kwargs["api_key"], "dashscope-offline-secret")

    def test_search_maps_real_api_schema_into_existing_citation_records(self):
        self.server.response_body = {"results": [None, {"url": ""}, {
            "title": "Source title", "url": "https://example.org/article", "summary": "Useful evidence",
            "date": "2026-10-19T00:00:00Z", "site_name": "Example"}]}
        records = tools.web_search_records("  test query  ", count=4)
        self.assertEqual(records, [{"source_id": "WEB-1", "title": "Source title", "url": "https://example.org/article",
                                   "snippet": "Useful evidence", "domain": "example.org", "source_type": "web",
                                   "published_at": "2026-10-19T00:00:00Z"}])
        call = self.server.calls[-1]
        self.assertEqual(call["path"], "/v1/tools/search")
        self.assertEqual(call["auth"], "Bearer deepseek-offline-secret")
        self.assertEqual(call["body"], {"query": "test query", "count": 4, "need_content": True})

    def test_search_handles_empty_results_and_uses_snippet_fallback(self):
        self.assertEqual(tools.web_search_records("nothing"), [])
        self.server.response_body = {"results": [{"url": "https://example.org", "snippet": "Short evidence"}]}
        self.assertEqual(tools.web_search_records("example")[0]["snippet"], "Short evidence")

    def test_search_bounds_request_and_propagates_authentication_errors(self):
        tools.web_search_records("研" * 200, count=100)
        self.assertEqual(len(self.server.calls[-1]["body"]["query"]), 128)
        self.assertEqual(self.server.calls[-1]["body"]["count"], 50)
        self.server.response_status = 401
        self.server.response_body = {"error": {"message": "invalid key"}}
        with self.assertRaisesRegex(RuntimeError, "401"):
            tools.web_search_records("authentication failure")

    def test_missing_search_key_fails_without_sending_request(self):
        os.environ.pop("DEEPSEEK_API_KEY")
        with self.assertRaisesRegex(ValueError, "DEEPSEEK_API_KEY"):
            tools.web_search_records("example")
        self.assertEqual(self.server.calls, [])

    def test_workflow_search_uses_the_selected_config_file(self):
        from mult_agents import main, graph
        from mult_agents.state import create_initial_state
        os.environ.pop("DEEPSEEK_API_KEY")
        os.environ.pop("DEEPSEEK_BASE_URL")
        os.environ["ENABLE_MILVUS"] = "false"
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "selected-config.json"
            config_path.write_text(json.dumps({
                "deepseek_api_key": "selected-file-offline-secret",
                "deepseek_base_url": self.base + "/v1",
            }), encoding="utf-8")
            config = AppConfig.from_file(config_path)
            with patch.object(main, "build_agent", return_value=None):
                agents = main.build_agents(config.model, config.api_key, config)
            app = graph.build_app(agents, None)
            state = create_initial_state("selected configuration probe", 1, "test-user", "test-tenant")
            result = app.nodes["web_search"].invoke(state)
        self.assertEqual(result["web_retrieval_stats"]["query_count"], 1)
        self.assertEqual(self.server.calls[-1]["path"], "/v1/tools/search")
        self.assertEqual(self.server.calls[-1]["auth"], "Bearer selected-file-offline-secret")
        self.assertNotIn("selected-file-offline-secret", repr(agents))

    def test_workflow_routes_embedding_and_summary_keys_independently(self):
        from mult_agents import main
        config = AppConfig.from_env()
        with patch.object(main, "MemoryManager") as constructor:
            main.build_memory_manager(config)
        self.assertEqual(constructor.call_args.kwargs["embedding_api_key"], "dashscope-offline-secret")
        self.assertEqual(constructor.call_args.kwargs["summary_api_key"], "deepseek-offline-secret")
        with patch.object(main, "init_rag_system") as rag, patch.object(main, "build_agent"):
            main.build_agents(config.model, config.api_key, config)
        self.assertEqual(rag.call_args.kwargs["api_key"], "dashscope-offline-secret")

    def test_memory_summary_uses_deepseek(self):
        from mult_agents.memory.manager import MemoryManager
        with tempfile.TemporaryDirectory() as temporary:
            try:
                memory = MemoryManager(db_path=str(Path(temporary) / "memory.db"), enable_milvus=False,
                                       embedding_api_key="dashscope-offline-secret", summary_api_key="deepseek-offline-secret",
                                       summary_model="deepseek-flash", deepseek_base_url=self.base + "/v1")
                self.assertEqual(memory._summary_llm.invoke("summarize").content, "offline answer")
            finally:
                # Existing SQLite stores release connection cycles on collection.
                gc.collect()
        self.assertEqual(self.server.calls[-1]["auth"], "Bearer deepseek-offline-secret")


if __name__ == "__main__":
    unittest.main()
