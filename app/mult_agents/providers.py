"""Shared DeepSeek chat configuration; embedding credentials remain independent."""
import os

from langchain_openai import ChatOpenAI
from .network import provider_base_url


def create_chat_model(api_key: str, model: str, temperature: float = 0.1, base_url: str | None = None):
    if not api_key or not api_key.strip():
        raise ValueError("缺少 DEEPSEEK_API_KEY，请在项目 .env 中填写 DeepSeek 密钥")
    endpoint = provider_base_url(base_url or os.getenv("DEEPSEEK_BASE_URL") or "https://api.deepseek.com")
    return ChatOpenAI(
        model=model,
        api_key=api_key,
        base_url=endpoint,
        temperature=temperature,
        timeout=90,
        max_retries=2,
        extra_body={"thinking": {"type": "disabled"}},
    )
