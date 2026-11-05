"""Release hardening checks use synthetic data and never call external models."""
import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from fastapi.testclient import TestClient
from app_main import create_app
from backend.service import get_workflow_service
from backend.service.workflow_service import WorkflowService
from mult_agents.config import AppConfig


class ReleaseSecurityTests(unittest.TestCase):
    def client(self):
        return TestClient(create_app(), base_url='http://127.0.0.1')

    def test_untrusted_host_is_rejected(self):
        self.assertEqual(self.client().get('/health', headers={'host': 'attacker.example'}).status_code, 400)

    def test_cross_origin_research_is_rejected(self):
        response = self.client().post('/api/v1/research/run', json={'query': 'sample'},
                                      headers={'origin': 'https://attacker.example'})
        self.assertEqual(response.status_code, 403)

    def test_simple_form_and_oversized_body_are_rejected(self):
        client = self.client()
        self.assertEqual(client.post('/api/v1/research/run', content='query=sample',
                                    headers={'content-type': 'application/x-www-form-urlencoded'}).status_code, 415)
        self.assertEqual(client.post('/api/v1/research/run', content=b'x' * 70000,
                                    headers={'content-type': 'application/json'}).status_code, 413)

    def test_query_and_identity_limits(self):
        from backend.schemas import ResearchRequest
        from pydantic import ValidationError
        for payload in ({'query': 'x' * 16001}, {'query': 'sample', 'user_id': 'x' * 129}):
            with self.assertRaises(ValidationError):
                ResearchRequest(**payload)

    def test_checkpoint_isolation_uses_complete_scope(self):
        service = WorkflowService('unused')
        service._ensure_initialized = Mock()
        service._memory_manager = None
        service._base_config = Mock()
        service._base_config.max_iterations = 1
        service._base_config.with_overrides.side_effect = lambda **v: SimpleNamespace(**v, enable_memory=False)
        service._app = Mock()
        service._app.invoke.return_value = {'final': 'synthetic answer'}
        keys = []
        for tenant, user in [('tenant-a', 'user'), ('tenant-b', 'user'), ('tenant-a', 'other-user')]:
            service._execute_sync('sample', user, 'shared-thread', tenant, 1, None)
            keys.append(service._app.invoke.call_args.args[1]['configurable']['thread_id'])
        self.assertEqual(len(set(keys)), 3)

    def test_http_and_sse_errors_do_not_expose_exception_text(self):
        app = create_app()
        service = Mock()
        service.run = AsyncMock(side_effect=RuntimeError('synthetic-secret-do-not-leak'))
        app.dependency_overrides[get_workflow_service] = lambda: service
        response = TestClient(app, base_url='http://127.0.0.1').post('/api/v1/research/run', json={'query': 'sample'})
        self.assertEqual(response.status_code, 500)
        self.assertNotIn('synthetic-secret-do-not-leak', response.text)
        real_service = WorkflowService('unused')
        real_service._run_sync_with_events = Mock(side_effect=RuntimeError('synthetic-secret-do-not-leak'))
        async def collect():
            return [event async for event in real_service.stream_events('sample', 'u', 't', 'tenant', 1, False)]
        events = asyncio.run(collect())
        self.assertNotIn('synthetic-secret-do-not-leak', str(events))
        self.assertTrue(any(e['type'] == 'error' for e in events))

    def test_config_repr_hides_credentials(self):
        config = AppConfig.from_file(require_llm=False).with_overrides(
            api_key='synthetic-chat-secret', embedding_api_key='synthetic-embedding-secret',
            postgres_dsn='postgresql://user:synthetic-db-secret@127.0.0.1/db')
        for secret in ['synthetic-chat-secret', 'synthetic-embedding-secret', 'synthetic-db-secret']:
            self.assertNotIn(secret, repr(config))

    def test_busy_worker_limit_rejects_and_releases_after_failure(self):
        from backend.service.workflow_service import ResearchBusyError
        service = WorkflowService('unused')
        service._research_slots.acquire()
        service._research_slots.acquire()
        with self.assertRaises(ResearchBusyError):
            service._run_sync('sample', 'u', 't', 'tenant', 1, False)
        service._research_slots.release()
        service._research_slots.release()
        service._record_and_execute = Mock(side_effect=RuntimeError('synthetic failure'))
        with self.assertRaises(RuntimeError):
            service._run_sync('sample', 'u', 't', 'tenant', 1, False)
        self.assertTrue(service._research_slots.acquire(blocking=False))
        self.assertTrue(service._research_slots.acquire(blocking=False))
        service._research_slots.release()
        service._research_slots.release()

    def test_chunked_body_limit_and_response_headers(self):
        from backend.security import LocalRequestGuard
        called = []
        async def downstream(scope, receive, send):
            called.append(True)
        chunks = iter([{'type': 'http.request', 'body': b'a' * 40000, 'more_body': True},
                       {'type': 'http.request', 'body': b'b' * 40000, 'more_body': False}])
        sent = []
        async def receive():
            return next(chunks)
        async def send(message):
            sent.append(message)
        scope = {'type': 'http', 'method': 'POST', 'path': '/',
                 'headers': [(b'content-type', b'application/json')]}
        asyncio.run(LocalRequestGuard(downstream, [])(scope, receive, send))
        self.assertFalse(called)
        self.assertEqual(sent[0]['status'], 413)
        response = self.client().get('/health')
        self.assertEqual(response.headers['x-content-type-options'], 'nosniff')
        self.assertEqual(response.headers['cache-control'], 'no-store')

    def test_provider_url_rejects_insecure_or_credentialed_destinations(self):
        from mult_agents.network import provider_base_url
        for value in ['file:///private', 'http://attacker.example', 'https://user:password@example.com',
                      'https://example.com?token=synthetic']:
            with self.assertRaises(ValueError):
                provider_base_url(value)
        self.assertEqual(provider_base_url('http://127.0.0.1:1234/v1'), 'http://127.0.0.1:1234/v1')

    def test_search_redirect_does_not_forward_credentials(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from threading import Thread
        from mult_agents.web_search import web_search_records
        paths = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                paths.append(self.path)
                self.send_response(302)
                self.send_header('Location', '/capture')
                self.end_headers()
            def do_GET(self):
                paths.append(self.path)
                self.send_response(200)
                self.end_headers()
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with self.assertRaises(RuntimeError):
                web_search_records('sample', api_key='synthetic-provider-key',
                                   base_url=f'http://127.0.0.1:{server.server_port}')
            self.assertEqual(paths, ['/v1/tools/search'])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_knowledge_and_personal_memory_cannot_share_a_collection(self):
        from mult_agents.main import build_agents
        config = AppConfig.from_file(require_llm=False)
        with self.assertRaises(ValueError):
            build_agents('synthetic', 'synthetic', config.with_overrides(
                enable_milvus=True, knowledge_collection='same', milvus_collection='same'))

    def test_rag_fallbacks_do_not_print_or_return_exception_secrets(self):
        import contextlib
        import io
        from unittest.mock import patch
        from mult_agents import tools
        from mult_agents.rag.core import RAGSystem
        output = io.StringIO()
        with patch.object(tools, '_RAG_SYSTEM', None), patch.object(tools, 'RAGSystem',
                side_effect=RuntimeError('synthetic-rag-secret')), contextlib.redirect_stdout(output):
            tools.init_rag_system('synthetic')
        self.assertNotIn('synthetic-rag-secret', output.getvalue())
        rag = object.__new__(RAGSystem)
        rag.search_records = Mock(side_effect=RuntimeError('synthetic-rag-secret'))
        self.assertNotIn('synthetic-rag-secret', rag.search('sample'))
