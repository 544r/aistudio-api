"""Route-level tests for the OpenAI and Anthropic compatibility endpoints."""

from __future__ import annotations

from collections.abc import AsyncGenerator

import httpx
import pytest

from aistudio_api.api.app import app
from aistudio_api.api.dependencies import get_client
from aistudio_api.application import thought_signatures
from aistudio_api.config import settings
from aistudio_api.domain.models import Candidate, ModelOutput


class FakeClient:
    async def generate_content(self, **_kwargs) -> ModelOutput:
        return ModelOutput(
            candidates=[Candidate(text="hello", thinking="think", finish_reason=1)],
            usage={
                "prompt_tokens": 2,
                "completion_tokens": 3,
                "total_tokens": 5,
            },
        )

    async def stream_generate_content(
        self, **_kwargs
    ) -> AsyncGenerator[tuple[str, object], None]:
        yield ("thinking", "think")
        yield ("thought_signature", "sig")
        yield ("body", "hello")
        yield ("usage", {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5})
        yield ("finish_reason", 1)
        yield ("done", None)


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


class ToolCallClient(FakeClient):
    async def generate_content(self, **_kwargs) -> ModelOutput:
        return ModelOutput(
            candidates=[
                Candidate(
                    function_calls=[
                        {
                            "name": "lookup",
                            "args": {"q": "x"},
                            "id": "call_1",
                            "thought_signature": "sig",
                        }
                    ],
                    finish_reason=1,
                )
            ],
            usage={
                "prompt_tokens": 2,
                "completion_tokens": 3,
                "total_tokens": 5,
            },
        )


@pytest.fixture(autouse=True)
def _fake_backend(monkeypatch):
    async def noop(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(
        "aistudio_api.application.api_service_compat.ensure_active_account", noop
    )
    thought_signatures.reset()
    app.dependency_overrides[get_client] = lambda: FakeClient()
    yield
    app.dependency_overrides.clear()
    thought_signatures.reset()


@pytest.mark.asyncio
async def test_openai_chat_completions_non_stream():
    async with _client() as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "gemini-3.7-flash",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    assert response.status_code == 200
    data = response.json()
    assert data["object"] == "chat.completion"
    assert data["choices"][0]["message"]["content"] == "hello"
    assert data["choices"][0]["message"]["reasoning_content"] == "think"
    assert data["usage"]["total_tokens"] == 5


@pytest.mark.asyncio
async def test_openai_chat_completions_stream():
    async with _client() as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "gemini-3.7-flash",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
                "stream_options": {"include_usage": True},
            },
        )
    assert response.status_code == 200
    text = response.text
    assert '"content": "hello"' in text
    assert '"reasoning_content": "think"' in text
    assert '"finish_reason": "stop"' in text
    assert "data: [DONE]" in text


@pytest.mark.asyncio
async def test_openai_chat_completions_rejects_invalid_body():
    async with _client() as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "gemini-3.7-flash", "messages": "not-a-list"},
        )
    assert response.status_code == 400
    assert response.json()["error"]["message"] == "messages must be an array"


@pytest.mark.asyncio
async def test_anthropic_messages_non_stream():
    async with _client() as client:
        response = await client.post(
            "/v1/messages",
            json={
                "model": "gemini-3.7-flash",
                "max_tokens": 128,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    assert response.status_code == 200
    data = response.json()
    assert data["type"] == "message"
    assert data["content"][0]["type"] == "thinking"
    assert data["content"][1] == {"type": "text", "text": "hello"}
    assert data["stop_reason"] == "end_turn"
    assert data["usage"]["input_tokens"] == 2


@pytest.mark.asyncio
async def test_anthropic_messages_stream():
    async with _client() as client:
        response = await client.post(
            "/v1/messages",
            json={
                "model": "gemini-3.7-flash",
                "max_tokens": 128,
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )
    assert response.status_code == 200
    text = response.text
    assert "event: message_start" in text
    assert '"type": "thinking_delta"' in text
    assert '"type": "text_delta"' in text
    assert "event: message_delta" in text
    assert "event: message_stop" in text


@pytest.mark.asyncio
async def test_anthropic_count_tokens():
    async with _client() as client:
        response = await client.post(
            "/v1/messages/count_tokens",
            json={
                "model": "gemini-3.7-flash",
                "max_tokens": 128,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    assert response.status_code == 200
    assert response.json()["input_tokens"] >= 1


@pytest.mark.asyncio
async def test_openai_auth_error_uses_openai_shape(monkeypatch):
    monkeypatch.setattr(settings, "api_keys", frozenset({"secret"}))
    async with _client() as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "gemini-3.7-flash",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    assert response.status_code == 401
    assert response.json()["error"]["type"] == "authentication_error"


@pytest.mark.asyncio
async def test_anthropic_auth_error_uses_anthropic_shape(monkeypatch):
    monkeypatch.setattr(settings, "api_keys", frozenset({"secret"}))
    async with _client() as client:
        response = await client.post(
            "/v1/messages",
            json={
                "model": "gemini-3.7-flash",
                "max_tokens": 128,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    assert response.status_code == 401
    body = response.json()
    assert body["type"] == "error"
    assert body["error"]["type"] == "authentication_error"


@pytest.mark.asyncio
async def test_v1_models_returns_anthropic_shape_for_anthropic_clients():
    async with _client() as client:
        response = await client.get(
            "/v1/models", headers={"anthropic-version": "2023-06-01"}
        )
    assert response.status_code == 200
    data = response.json()
    assert "has_more" in data
    assert data["data"][0]["type"] == "model"


@pytest.mark.asyncio
async def test_openai_response_carries_thought_signature():
    app.dependency_overrides[get_client] = lambda: ToolCallClient()
    async with _client() as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "gemini-3.7-flash",
                "messages": [{"role": "user", "content": "look it up"}],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "lookup",
                            "parameters": {"type": "object", "properties": {}},
                        },
                    }
                ],
            },
        )
    assert response.status_code == 200
    data = response.json()
    tool_call = data["choices"][0]["message"]["tool_calls"][0]
    assert tool_call["extra_content"] == {
        "google": {"thought_signature": "sig"}
    }
    assert data["choices"][0]["finish_reason"] == "tool_calls"


@pytest.mark.asyncio
async def test_anthropic_response_carries_thought_signature_as_redacted_thinking():
    app.dependency_overrides[get_client] = lambda: ToolCallClient()
    async with _client() as client:
        response = await client.post(
            "/v1/messages",
            json={
                "model": "gemini-3.7-flash",
                "max_tokens": 128,
                "messages": [{"role": "user", "content": "look it up"}],
                "tools": [
                    {
                        "name": "lookup",
                        "input_schema": {"type": "object", "properties": {}},
                    }
                ],
            },
        )
    assert response.status_code == 200
    data = response.json()
    redacted = [
        block
        for block in data["content"]
        if block.get("type") == "redacted_thinking"
    ]
    assert redacted
    assert redacted[0]["data"] == "sig"
    assert data["stop_reason"] == "tool_use"
