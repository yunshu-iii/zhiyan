"""Provider URL validation and redirects that cannot forward credentials."""
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler


def provider_base_url(value: str) -> str:
    parsed = urlsplit(value)
    local_http = parsed.scheme == 'http' and parsed.hostname in {'127.0.0.1', 'localhost', '::1'}
    if (parsed.scheme != 'https' and not local_http) or not parsed.hostname:
        raise ValueError('模型服务地址须使用 HTTPS；仅本机测试允许 HTTP。')
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('模型服务地址不能包含凭据、查询参数或片段。')
    return value.rstrip('/')


class NoCredentialRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Fail closed rather than forwarding the provider's Authorization header.
        return None
