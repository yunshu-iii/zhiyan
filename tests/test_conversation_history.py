"""History API and workflow regression tests; execution is mocked, no paid API calls."""
import asyncio
import json
import sys
import unittest
from pathlib import Path
from uuid import uuid4
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
from fastapi.testclient import TestClient
from app_main import create_app


class ConversationHistoryTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(create_app(), base_url='http://127.0.0.1')
        self.scope = {'tenant_id': 'history-test-' + uuid4().hex, 'user_id': 'test-user'}
        self.addCleanup(self.cleanup)

    def cleanup(self):
        from backend.service import get_history_store
        store = get_history_store()
        with store.connect() as conn:
            for table in ('chat_messages', 'chat_sessions', 'short_term_messages'):
                conn.execute('DELETE FROM ' + table + ' WHERE tenant_id=%s AND user_id=%s',
                             (self.scope['tenant_id'], self.scope['user_id']))

    def create(self):
        response = self.client.post('/api/v1/conversations', json=self.scope)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_history_api_is_available_without_running_a_model(self):
        response = self.client.get('/api/v1/conversations', params=self.scope)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {'conversations': [], 'total': 0})

    def test_new_sessions_have_independent_ids_and_scoped_access(self):
        first, second = self.create(), self.create()
        self.assertNotEqual(first['thread_id'], second['thread_id'])
        other = {**self.scope, 'user_id': 'other-user'}
        self.assertEqual(self.client.get('/api/v1/conversations/' + first['thread_id'], params=other).status_code, 404)
        self.assertEqual(self.client.get('/api/v1/conversations', params=other).json()['total'], 0)
        other = {**self.scope, 'tenant_id': 'other-tenant'}
        self.assertEqual(self.client.get('/api/v1/conversations/' + first['thread_id'], params=other).status_code, 404)

    def test_history_preserves_full_messages_and_paginates(self):
        from backend.service import get_history_store
        store = get_history_store()
        first, second = self.create(), self.create()
        for index in range(36):
            store.append_message(**self.scope, thread_id=first['thread_id'], role='user' if index % 2 == 0 else 'assistant', content=f'message {index}')
        page = self.client.get('/api/v1/conversations', params={**self.scope, 'limit': 1}).json()
        self.assertEqual(page['total'], 2)
        self.assertEqual(page['conversations'][0]['thread_id'], first['thread_id'])
        self.assertEqual(page['conversations'][0]['message_count'], 36)
        page2 = self.client.get('/api/v1/conversations', params={**self.scope, 'limit': 1, 'offset': 1}).json()
        self.assertEqual(page2['conversations'][0]['thread_id'], second['thread_id'])
        detail = self.client.get('/api/v1/conversations/' + first['thread_id'], params=self.scope).json()
        self.assertEqual([m['content'] for m in detail['messages']], [f'message {i}' for i in range(36)])
        self.assertEqual(detail['conversation']['title'], 'message 0')

    def test_legacy_import_is_preserved_and_not_repeated_after_restart(self):
        from backend.service import get_history_store
        from backend.service.history_store import HistoryStore
        store = get_history_store()
        thread = 'legacy-' + uuid4().hex
        with store.connect() as conn:
            for role, content in [('human', 'Legacy question'), ('ai', 'Legacy answer'),
                                  ('human', 'Second question'), ('ai', 'Second answer')]:
                conn.execute('INSERT INTO short_term_messages (id,tenant_id,user_id,thread_id,role,content) VALUES (%s,%s,%s,%s,%s,%s)',
                             (str(uuid4()), self.scope['tenant_id'], self.scope['user_id'], thread, role, content))
        restarted = HistoryStore(store.dsn)
        detail = restarted.get_conversation(**self.scope, thread_id=thread)
        self.assertEqual([m['content'] for m in detail['messages']],
                         ['Legacy question', 'Legacy answer', 'Second question', 'Second answer'])
        with store.connect() as conn:
            conn.execute('DELETE FROM short_term_messages WHERE tenant_id=%s AND user_id=%s', tuple(self.scope.values()))
            conn.execute('INSERT INTO short_term_messages (id,tenant_id,user_id,thread_id,role,content) VALUES (%s,%s,%s,%s,%s,%s)',
                         (str(uuid4()), self.scope['tenant_id'], self.scope['user_id'], thread, 'human', 'Must not re-import'))
        again = HistoryStore(store.dsn).get_conversation(**self.scope, thread_id=thread)
        self.assertEqual(len(again['messages']), 4)

    def test_completed_and_failed_requests_are_archived_independently_of_memory(self):
        from backend.service import get_history_store
        from backend.service.workflow_service import WorkflowService
        store = get_history_store()
        service = WorkflowService(config_path='unused-in-mocked-execution', history_store=store)
        thread = self.create()['thread_id']
        args = dict(query='Offline question', **self.scope, thread_id=thread, max_iterations=1, enable_memory=False)
        with patch.object(service, '_execute_sync', return_value=('Offline answer', 'direct')):
            self.assertEqual(service._run_sync(**args)[0], 'Offline answer')
        with patch.object(service, '_execute_sync', side_effect=RuntimeError('offline failure')):
            with self.assertRaises(RuntimeError): service._run_sync(**{**args, 'query': 'Failed question'})
        detail = store.get_conversation(**self.scope, thread_id=thread)
        self.assertEqual([m['role'] for m in detail['messages']], ['user', 'assistant', 'user', 'assistant'])
        self.assertEqual(detail['messages'][1]['content'], 'Offline answer')
        self.assertIn('失败', detail['messages'][3]['content'])

    def test_streaming_path_archives_final_answer(self):
        from backend.service import get_history_store
        from backend.service.workflow_service import WorkflowService
        store = get_history_store()
        service = WorkflowService(config_path='unused-in-mocked-execution', history_store=store)
        thread = self.create()['thread_id']
        async def collect():
            return [event async for event in service.stream_events(query='Stream question', **self.scope, thread_id=thread, max_iterations=1, enable_memory=False)]
        with patch.object(service, '_execute_sync_with_events', return_value=('Stream answer', 'direct')):
            events = asyncio.run(collect())
        self.assertEqual(next(e['final'] for e in events if e['type'] == 'final'), 'Stream answer')
        detail = store.get_conversation(**self.scope, thread_id=thread)
        self.assertEqual([m['content'] for m in detail['messages']], ['Stream question', 'Stream answer'])

    def test_successful_answer_survives_model_memory_write_failure(self):
        from backend.service import get_history_store
        from backend.service.workflow_service import WorkflowService
        from mult_agents.config import AppConfig
        store = get_history_store()
        for streaming in (False, True):
            with self.subTest(streaming=streaming):
                thread = self.create()['thread_id']
                service = WorkflowService(config_path='unused', history_store=store)
                service._initialized = True
                service._base_config = AppConfig.from_file(require_llm=False)
                service._memory_manager = Mock()
                service._memory_manager.build_personalized_prompt_context.return_value = ''
                service._memory_manager.persist_turn.side_effect = RuntimeError('simulated compression failure')
                service._app = Mock()
                service._app.invoke.return_value = {'final': 'Preserved answer', 'intent': 'direct'}
                service._app.stream.return_value = iter([{'write': {'final': 'Preserved answer'}}])
                args = dict(query='Preserve my answer', **self.scope, thread_id=thread, max_iterations=1, enable_memory=True)
                result = service._run_sync_with_events(**args, emit=lambda event: None) if streaming else service._run_sync(**args)
                self.assertEqual(result[0], 'Preserved answer')
                detail = store.get_conversation(**self.scope, thread_id=thread)
                self.assertEqual(detail['messages'][-1]['content'], 'Preserved answer')


if __name__ == '__main__':
    unittest.main()
