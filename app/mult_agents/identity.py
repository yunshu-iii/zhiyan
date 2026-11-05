"""Stable checkpoint identifiers including all application scope fields."""
import hashlib
import json


def checkpoint_thread_id(tenant_id: str, user_id: str, thread_id: str) -> str:
    scope = json.dumps([tenant_id, user_id, thread_id], ensure_ascii=False, separators=(',', ':'))
    return 'scope-' + hashlib.sha256(scope.encode('utf-8')).hexdigest()
