"""Executable protocols and purposes for the shared Profile client."""
from urllib.parse import urlsplit


def profile_protocol_error(provider: str, base_url: str | None) -> str | None:
    if provider in ("openai", "openai_compatible"):
        return None
    if provider == "ollama" and urlsplit(base_url or "").path.rstrip("/").endswith("/v1"):
        return None
    if provider == "ollama":
        return "Ollama Profile 仅支持 OpenAI 兼容接口，请将地址设置为以 /v1 结尾的兼容地址"
    return "此 Profile 客户端尚不支持该原生协议；请使用公式 AI 的原生设置，或选择 OpenAI 兼容接口并填写匹配的兼容地址"
