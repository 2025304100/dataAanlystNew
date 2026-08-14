from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

from app.api.routes import ai_config


pytestmark = pytest.mark.whitebox


def test_ai_config_persists_masks_and_preserves_secret(tmp_path, monkeypatch):
    config_path = tmp_path / "ai_config.json"
    monkeypatch.setattr(ai_config, "AI_CONFIG_PATH", config_path)

    result = ai_config.update_ai_config(
        ai_config.AiConfigUpdate(
            provider="openai_compatible",
            service_url="https://example.test/v1/",
            api_key="secret-key-123",
            model="demo-model",
            enabled=False,
        )
    )

    assert result["persisted"] is True
    assert config_path.exists()
    stored = json.loads(config_path.read_text(encoding="utf-8"))
    assert stored["service_url"] == "https://example.test/v1"
    assert stored["api_key"] == "secret-key-123"
    assert stored["version"] == 2

    public = ai_config.get_ai_config()
    assert public.persisted is True
    assert public.api_key_set is True
    assert public.api_key != "secret-key-123"
    assert "****" in public.api_key

    ai_config.update_ai_config(
        ai_config.AiConfigUpdate(
            model="updated-model",
        )
    )
    reloaded = json.loads(config_path.read_text(encoding="utf-8"))
    assert reloaded["api_key"] == "secret-key-123"
    assert reloaded["model"] == "updated-model"
    assert reloaded["service_url"] == "https://example.test/v1"


def test_enabled_config_requires_key_unless_auth_is_none(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_config, "AI_CONFIG_PATH", tmp_path / "ai_config.json")

    with pytest.raises(HTTPException) as exc_info:
        ai_config.update_ai_config(
            ai_config.AiConfigUpdate(
                service_url="https://example.test/v1",
                api_key="",
                enabled=True,
                auth_type="bearer",
            )
        )
    assert exc_info.value.status_code == 400

    result = ai_config.update_ai_config(
        ai_config.AiConfigUpdate(
            provider="ollama",
            service_url="http://127.0.0.1:11434",
            api_key="",
            model="qwen3",
            enabled=True,
            auth_type="none",
            chat_path="/api/chat",
            models_path="/api/tags",
        )
    )
    assert result["persisted"] is True


def test_provider_headers_payloads_and_responses():
    anthropic = ai_config._normalized_config({
        "provider": "anthropic",
        "service_url": "https://api.anthropic.com/v1",
        "api_key": "anthropic-secret",
        "auth_type": "x-api-key",
        "model": "claude-test",
    }, saved={"api_key": ""})
    headers = ai_config._auth_headers(anthropic, json_content=True)
    assert headers["x-api-key"] == "anthropic-secret"
    assert headers["anthropic-version"] == "2023-06-01"
    payload = ai_config._build_chat_payload(
        anthropic,
        [
            {"role": "system", "content": "system rules"},
            {"role": "user", "content": "hello"},
        ],
    )
    assert payload["system"] == "system rules"
    assert payload["messages"] == [{"role": "user", "content": "hello"}]

    ollama = ai_config._normalized_config({
        "provider": "ollama",
        "service_url": "http://127.0.0.1:11434",
        "auth_type": "none",
        "model": "qwen3",
        "chat_path": "/api/chat",
    }, saved={"api_key": ""})
    assert ai_config._auth_headers(ollama) == {}
    assert ai_config._endpoint_url(ollama, "chat_path") == "http://127.0.0.1:11434/api/chat"
    assert ai_config._build_chat_payload(
        ollama, [{"role": "user", "content": "hello"}]
    )["stream"] is False
    assert ai_config._build_chat_payload(
        ollama, [{"role": "user", "content": "hello"}], stream=True
    )["stream"] is True

    assert ai_config._extract_ai_reply(
        {"choices": [{"message": {"content": "openai"}}]}
    ) == "openai"
    assert ai_config._extract_ai_reply(
        {"content": [{"type": "text", "text": "anthropic"}]}
    ) == "anthropic"
    assert ai_config._extract_ai_reply(
        {"message": {"content": "ollama"}}
    ) == "ollama"

    openai_payload = ai_config._build_chat_payload(
        anthropic,
        [{"role": "system", "content": "system rules"}, {"role": "user", "content": "hello"}],
        stream=True,
    )
    assert openai_payload["stream"] is True


def test_ai_chat_stream_delta_parser_accepts_common_shapes():
    assert ai_config._extract_stream_delta(
        'data: {"choices":[{"delta":{"content":"hello"}}]}'
    ) == ("hello", False)
    assert ai_config._extract_stream_delta(
        '{"message":{"content":"ollama"},"done":false}'
    ) == ("ollama", False)
    assert ai_config._extract_stream_delta(
        '{"type":"content_block_delta","delta":{"text":"anthropic"}}'
    ) == ("anthropic", False)
    assert ai_config._extract_stream_delta("data: [DONE]") == ("", True)


def test_model_parser_accepts_openai_and_ollama_shapes():
    openai_models = ai_config._parse_model_list(
        {"data": [{"id": "gpt-test", "owned_by": "vendor", "created": 2}]}
    )
    ollama_models = ai_config._parse_model_list(
        {"models": [{"name": "qwen3:latest", "details": {"family": "qwen3"}}]}
    )

    assert openai_models[0]["id"] == "gpt-test"
    assert ollama_models[0]["id"] == "qwen3:latest"
    assert ollama_models[0]["owned_by"] == "qwen3"


def test_factor_formula_chat_mode_uses_factor_catalog():
    request = ai_config.AiChatRequest(
        message="写一个换手率标准分公式",
        formula="turnover_rate",
        formula_mode="factor",
    )
    assert request.formula_mode == "factor"
    prompt = ai_config._FACTOR_SYSTEM_FUNCTIONS_DOC
    assert "sma(field,n)" in prompt
    assert "pe_ttm" in prompt
    assert "回看窗口为 1-250 个交易日" in prompt
    assert "```formula" in prompt
