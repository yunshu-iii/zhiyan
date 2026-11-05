"""DeepSeek Search API adapter for the workflow's existing evidence format."""
import json
import logging
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from .config import AppConfig
from .network import NoCredentialRedirect, provider_base_url

logger = logging.getLogger(__name__)


def web_search_records(
    query: str, count: int = 8, *, api_key: str | None = None, base_url: str | None = None
) -> list[dict]:
    query = query.strip()[:128]
    if not query:
        return []
    if api_key is None:
        config = AppConfig.from_file()
        api_key = config.api_key
        base_url = base_url or config.deepseek_base_url
    if not api_key.strip():
        raise ValueError("缺少 DEEPSEEK_API_KEY，请在项目 .env 中填写 DeepSeek 密钥")
    count = max(1, min(int(count), 50))
    base = provider_base_url(base_url or "https://api.deepseek.com")
    if base.endswith("/v1"):
        base = base[:-3]
    request = urllib.request.Request(
        url=base + "/v1/tools/search",
        data=json.dumps({"query": query, "count": count, "need_content": True}, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.build_opener(NoCredentialRedirect()).open(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"DeepSeek 搜索请求失败（HTTP {exc.code}），请检查密钥、余额和接口权限") from None
    except urllib.error.URLError:
        raise RuntimeError("DeepSeek 搜索网络连接失败，请检查网络和 DEEPSEEK_BASE_URL") from None
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise RuntimeError("DeepSeek 搜索返回了无效 JSON") from None
    pages = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(pages, list):
        raise RuntimeError("DeepSeek 搜索响应缺少 results 数组")
    records = []
    for page in pages:
        if not isinstance(page, dict):
            continue
        url = str(page.get("url") or "").strip()
        try:
            parsed = urlsplit(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                continue
        except ValueError:
            continue
        records.append({
            "source_id": f"WEB-{len(records) + 1}",
            "title": str(page.get("title") or parsed.hostname),
            "url": url,
            "snippet": str(page.get("summary") or page.get("snippet") or page.get("content") or ""),
            "domain": parsed.hostname,
            "source_type": "web",
            "published_at": page.get("date") or "",
        })
        if len(records) >= count:
            break
    logger.info("web_search.web_search_records: event")
    return records
