"""Request boundaries for the local, single-operator application.

These checks do not provide user authentication or public multi-tenant authorization.
"""
from starlette.responses import JSONResponse


class LocalRequestGuard:
    def __init__(self, app, allowed_origins, max_body_bytes=65536):
        self.app = app
        self.allowed_origins = set(allowed_origins)
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        headers = {k.lower(): v for k, v in scope.get('headers', [])}
        origin = headers.get(b'origin', b'').decode('latin-1')
        if origin and origin not in self.allowed_origins:
            return await JSONResponse({'detail': 'Origin not allowed'}, status_code=403)(scope, receive, send)
        buffered = []
        if scope['method'] in {'POST', 'PUT', 'PATCH'}:
            media_type = headers.get(b'content-type', b'').split(b';', 1)[0].strip().lower()
            if media_type != b'application/json':
                return await JSONResponse({'detail': 'JSON required'}, status_code=415)(scope, receive, send)
            size = 0
            while True:
                message = await receive()
                if message['type'] == 'http.disconnect':
                    return
                size += len(message.get('body', b''))
                if size > self.max_body_bytes:
                    return await JSONResponse({'detail': 'Request body too large'}, status_code=413)(scope, receive, send)
                buffered.append(message)
                if not message.get('more_body', False):
                    break

        async def replay():
            return buffered.pop(0) if buffered else await receive()

        async def secure_send(message):
            if message['type'] == 'http.response.start':
                response_headers = list(message.get('headers', []))
                response_headers.extend([(b'x-content-type-options', b'nosniff'),
                                         (b'cache-control', b'no-store'),
                                         (b'referrer-policy', b'no-referrer')])
                message = {**message, 'headers': response_headers}
            await send(message)
        await self.app(scope, replay, secure_send)
