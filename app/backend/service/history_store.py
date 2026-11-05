"""Full conversation archive, independent of model-memory compression."""
from contextlib import contextmanager
from threading import Lock
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row


class HistoryStore:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self._ready = False
        self._lock = Lock()

    @contextmanager
    def connect(self):
        with psycopg.connect(self.dsn, row_factory=dict_row, connect_timeout=10) as conn:
            yield conn

    def _ensure_schema(self):
        if self._ready:
            return
        with self._lock:
            if self._ready:
                return
            with self.connect() as conn:
                # Serialize schema setup and legacy import across app workers.
                conn.execute('SELECT pg_advisory_xact_lock(748391205)')
                conn.execute('''CREATE TABLE IF NOT EXISTS chat_sessions (
                    tenant_id TEXT NOT NULL, user_id TEXT NOT NULL, thread_id TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '新会话',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (tenant_id,user_id,thread_id))''')
                conn.execute('''CREATE TABLE IF NOT EXISTS chat_messages (
                    sequence BIGSERIAL UNIQUE, id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL, user_id TEXT NOT NULL, thread_id TEXT NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('user','assistant')),
                    content TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    FOREIGN KEY (tenant_id,user_id,thread_id)
                    REFERENCES chat_sessions(tenant_id,user_id,thread_id))''')
                conn.execute('''CREATE INDEX IF NOT EXISTS idx_chat_messages_scope
                    ON chat_messages(tenant_id,user_id,thread_id,sequence)''')
                conn.execute('''CREATE INDEX IF NOT EXISTS idx_chat_sessions_updated
                    ON chat_sessions(tenant_id,user_id,updated_at DESC)''')
                if conn.execute("SELECT to_regclass('short_term_messages') AS table_name").fetchone()['table_name']:
                    scopes = conn.execute('''SELECT DISTINCT m.tenant_id,m.user_id,m.thread_id
                        FROM short_term_messages m WHERE NOT EXISTS (
                            SELECT 1 FROM chat_sessions s WHERE s.tenant_id=m.tenant_id
                            AND s.user_id=m.user_id AND s.thread_id=m.thread_id)''').fetchall()
                    for scope in scopes:
                        identity = (scope['tenant_id'], scope['user_id'], scope['thread_id'])
                        messages = conn.execute('''SELECT id,role,content,created_at FROM short_term_messages
                            WHERE tenant_id=%s AND user_id=%s AND thread_id=%s
                            ORDER BY created_at,ctid''', identity).fetchall()
                        # Legacy compression used one transaction timestamp for every retained row.
                        # The legacy table has no sequence; physical row order is the available tie-breaker.
                        if not messages:
                            continue
                        question = next((m['content'] for m in messages if m['role']=='human'), '历史会话')
                        title = self._title(question)
                        conn.execute('''INSERT INTO chat_sessions
                            (tenant_id,user_id,thread_id,title,created_at,updated_at)
                            VALUES (%s,%s,%s,%s,%s,%s)''', (*identity,title,messages[0]['created_at'],messages[-1]['created_at']))
                        for message in messages:
                            if message['role'] not in ('human','ai'):
                                continue
                            conn.execute('''INSERT INTO chat_messages
                                (id,tenant_id,user_id,thread_id,role,content,created_at)
                                VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING''',
                                (message['id'],*identity,'user' if message['role']=='human' else 'assistant',message['content'],message['created_at']))
            self._ready = True

    @staticmethod
    def _title(content: str) -> str:
        return ' '.join(content.split())[:60] or '新会话'

    def create_conversation(self, tenant_id: str, user_id: str) -> dict:
        self._ensure_schema()
        thread_id = str(uuid4())
        with self.connect() as conn:
            row = conn.execute('''INSERT INTO chat_sessions (tenant_id,user_id,thread_id)
                VALUES (%s,%s,%s) RETURNING thread_id,title,created_at,updated_at''',
                (tenant_id,user_id,thread_id)).fetchone()
        return {**row, 'message_count': 0}

    def append_message(self, tenant_id: str, user_id: str, thread_id: str, role: str, content: str):
        self._ensure_schema()
        identity = (tenant_id,user_id,thread_id)
        with self.connect() as conn:
            conn.execute('''INSERT INTO chat_sessions (tenant_id,user_id,thread_id)
                VALUES (%s,%s,%s) ON CONFLICT DO NOTHING''', identity)
            conn.execute('''SELECT thread_id FROM chat_sessions
                WHERE tenant_id=%s AND user_id=%s AND thread_id=%s FOR UPDATE''', identity)
            empty = not conn.execute('''SELECT 1 FROM chat_messages
                WHERE tenant_id=%s AND user_id=%s AND thread_id=%s LIMIT 1''', identity).fetchone()
            conn.execute('''INSERT INTO chat_messages (id,tenant_id,user_id,thread_id,role,content)
                VALUES (%s,%s,%s,%s,%s,%s)''', (str(uuid4()),*identity,role,content))
            if empty and role == 'user':
                conn.execute('''UPDATE chat_sessions SET title=%s,updated_at=NOW()
                    WHERE tenant_id=%s AND user_id=%s AND thread_id=%s''', (self._title(content),*identity))
            else:
                conn.execute('''UPDATE chat_sessions SET updated_at=NOW()
                    WHERE tenant_id=%s AND user_id=%s AND thread_id=%s''', identity)

    def list_conversations(self, tenant_id: str, user_id: str, limit: int = 50, offset: int = 0) -> dict:
        self._ensure_schema()
        with self.connect() as conn:
            total = conn.execute('SELECT COUNT(*) AS count FROM chat_sessions WHERE tenant_id=%s AND user_id=%s',
                                 (tenant_id,user_id)).fetchone()['count']
            rows = conn.execute('''SELECT s.thread_id,s.title,s.created_at,s.updated_at,
                (SELECT COUNT(*) FROM chat_messages m WHERE m.tenant_id=s.tenant_id
                    AND m.user_id=s.user_id AND m.thread_id=s.thread_id) AS message_count
                FROM chat_sessions s WHERE s.tenant_id=%s AND s.user_id=%s
                ORDER BY s.updated_at DESC,s.thread_id LIMIT %s OFFSET %s''',
                (tenant_id,user_id,limit,offset)).fetchall()
        return {'conversations': rows, 'total': total}

    def get_conversation(self, tenant_id: str, user_id: str, thread_id: str) -> dict | None:
        self._ensure_schema()
        identity = (tenant_id,user_id,thread_id)
        with self.connect() as conn:
            session = conn.execute('''SELECT thread_id,title,created_at,updated_at FROM chat_sessions
                WHERE tenant_id=%s AND user_id=%s AND thread_id=%s''', identity).fetchone()
            if session is None:
                return None
            messages = conn.execute('''SELECT id,role,content,created_at FROM chat_messages
                WHERE tenant_id=%s AND user_id=%s AND thread_id=%s ORDER BY sequence''', identity).fetchall()
        return {'conversation': {**session, 'message_count': len(messages)}, 'messages': messages}
