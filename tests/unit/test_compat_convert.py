"""Unit tests for the OpenAI/Anthropic compatibility converters."""

# JSON assertions in this file intentionally index dynamic payload dictionaries.
# pyright: reportIndexIssue=false, reportOptionalSubscript=false, reportGeneralTypeIssues=false

from __future__ import annotations

import json

import pytest

from aistudio_api.api.compat.anthropic import (
    AnthropicStreamEncoder,
    response_payload as anthropic_response_payload,
)
from aistudio_api.api.compat.convert import anthropic_to_gemini, openai_to_gemini
from aistudio_api.api.compat.openai import response_payload as openai_response_payload
from aistudio_api.application import thought_signatures
from aistudio_api.application.chat_service import normalize_gemini_request
from aistudio_api.domain.models import Candidate, ModelOutput


@pytest.fixture(autouse=True)
def _clear_thought_signatures():
    thought_signatures.reset()
    yield
    thought_signatures.reset()


def test_compat_requests_do_not_inject_configured_default_tools() -> None:
    req = openai_to_gemini(
        {
            "model": "gemini-3.7-flash",
            "messages": [{"role": "user", "content": "hi"}],
        }
    )
    normalized = normalize_gemini_request(
        req, "gemini-3.7-flash", use_default_tools=False
    )
    assert normalized.tools is None


def test_openai_request_converts_messages_images_and_tools() -> None:
    req = openai_to_gemini(
        {
            "model": "gemini-3.7-flash",
            "messages": [
                {"role": "system", "content": "be brief"},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "describe"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64,AA=="},
                        },
                    ],
                },
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "lookup",
                                "arguments": json.dumps({"q": "x"}),
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_1",
                    "content": json.dumps({"ok": True}),
                },
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "lookup",
                        "description": "look something up",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
            "tool_choice": "required",
            "temperature": 0.2,
            "reasoning_effort": "medium",
        }
    )

    assert [content.role for content in req.contents] == ["user", "model", "user"]
    assert req.systemInstruction is not None
    assert req.systemInstruction.parts[0].text == "be brief"
    assert req.contents[0].parts[0].text == "describe"
    assert req.contents[0].parts[1].inlineData is not None
    assert req.contents[0].parts[1].inlineData.mimeType == "image/png"
    assert req.contents[1].parts[0].functionCall is not None
    assert req.contents[1].parts[0].functionCall.name == "lookup"
    assert req.contents[1].parts[0].functionCall.args == {"q": "x"}
    assert req.contents[2].parts[0].functionResponse is not None
    assert req.contents[2].parts[0].functionResponse.name == "lookup"
    assert req.tools is not None
    assert req.tools[0].functionDeclarations[0]["name"] == "lookup"
    assert req.toolConfig == {
        "functionCallingConfig": {"mode": "ANY"}
    }
    assert req.generationConfig is not None
    assert req.generationConfig.temperature == 0.2
    assert req.generationConfig.thinkingConfig == {"level": "MEDIUM", "mode": 1}


def test_anthropic_request_converts_tools_and_thinking() -> None:
    req = anthropic_to_gemini(
        {
            "model": "gemini-3.7-flash",
            "max_tokens": 1024,
            "system": "be brief",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "describe"},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": "AA==",
                            },
                        },
                    ],
                },
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_1",
                            "name": "lookup",
                            "input": {"q": "x"},
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_1",
                            "content": "ok",
                        }
                    ],
                },
            ],
            "tools": [
                {
                    "name": "lookup",
                    "description": "look something up",
                    "input_schema": {"type": "object", "properties": {}},
                }
            ],
            "tool_choice": {"type": "tool", "name": "lookup"},
            "thinking": {"type": "enabled", "budget_tokens": 2048},
        }
    )

    assert [content.role for content in req.contents] == ["user", "model", "user"]
    assert req.systemInstruction is not None
    assert req.systemInstruction.parts[0].text == "be brief"
    assert req.contents[0].parts[1].inlineData is not None
    assert req.contents[1].parts[0].functionCall is not None
    assert req.contents[1].parts[0].functionCall.id == "toolu_1"
    assert req.contents[2].parts[0].functionResponse is not None
    assert req.contents[2].parts[0].functionResponse.name == "lookup"
    assert req.contents[2].parts[0].functionResponse.response == {"result": "ok"}
    assert req.tools is not None
    assert req.tools[0].functionDeclarations[0]["name"] == "lookup"
    assert req.toolConfig == {
        "functionCallingConfig": {
            "mode": "ANY",
            "allowedFunctionNames": ["lookup"],
        }
    }
    assert req.generationConfig is not None
    assert req.generationConfig.maxOutputTokens == 1024
    assert req.generationConfig.thinkingConfig == {"level": "MEDIUM", "mode": 1}


def test_openai_mid_conversation_system_message_keeps_position() -> None:
    req = openai_to_gemini(
        {
            "model": "gemini-3.7-flash",
            "messages": [
                {"role": "system", "content": "top-level instruction"},
                {"role": "user", "content": "hello"},
                {"role": "system", "content": "mid-conversation control"},
                {"role": "user", "content": "continue"},
            ],
        }
    )

    assert req.systemInstruction is not None
    assert req.systemInstruction.parts[0].text == "top-level instruction"
    assert len(req.contents) == 1
    assert [part.text for part in req.contents[0].parts] == [
        "hello",
        "mid-conversation control",
        "continue",
    ]


def test_openai_history_restores_thought_signature() -> None:
    thought_signatures.remember(
        [
            {
                "id": "call_1",
                "name": "lookup",
                "args": {"q": "x"},
                "thought_signature": "sig",
            }
        ]
    )
    req = openai_to_gemini(
        {
            "model": "gemini-3.7-flash",
            "messages": [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "lookup",
                                "arguments": json.dumps({"q": "x"}),
                            },
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
            ],
        }
    )

    assert req.contents[0].role == "model"
    assert req.contents[0].parts[0].functionCall is not None
    assert req.contents[0].parts[0].thoughtSignature == "sig"


def test_openai_extra_content_signature_is_used_and_remembered() -> None:
    req = openai_to_gemini(
        {
            "model": "gemini-3.7-flash",
            "messages": [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_2",
                            "type": "function",
                            "function": {
                                "name": "lookup",
                                "arguments": json.dumps({"q": "x"}),
                            },
                            "extra_content": {
                                "google": {"thought_signature": "sig2"}
                            },
                        }
                    ],
                }
            ],
        }
    )

    assert req.contents[0].parts[0].thoughtSignature == "sig2"
    assert (
        thought_signatures.lookup("call_2", "lookup", {"q": "x"}) == "sig2"
    )


def test_anthropic_history_restores_thought_signature() -> None:
    thought_signatures.remember(
        [
            {
                "id": "toolu_1",
                "name": "lookup",
                "args": {"q": "x"},
                "thought_signature": "sig",
            }
        ]
    )
    req = anthropic_to_gemini(
        {
            "model": "gemini-3.7-flash",
            "max_tokens": 128,
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_1",
                            "name": "lookup",
                            "input": {"q": "x"},
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_1",
                            "content": "ok",
                        }
                    ],
                },
            ],
        }
    )

    assert req.contents[0].role == "model"
    assert req.contents[0].parts[0].functionCall is not None
    assert req.contents[0].parts[0].thoughtSignature == "sig"


def test_anthropic_redacted_thinking_signature_is_used() -> None:
    req = anthropic_to_gemini(
        {
            "model": "gemini-3.7-flash",
            "max_tokens": 128,
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "redacted_thinking", "data": "sig3"},
                        {
                            "type": "tool_use",
                            "id": "toolu_2",
                            "name": "lookup",
                            "input": {"q": "x"},
                        },
                    ],
                }
            ],
        }
    )

    assert req.contents[0].parts[0].thoughtSignature == "sig3"


def _output(*, thinking: str = "", text: str = "", calls: list[dict] | None = None) -> ModelOutput:
    return ModelOutput(
        candidates=[
            Candidate(
                text=text,
                thinking=thinking,
                function_calls=calls or [],
                finish_reason=1,
            )
        ],
        usage={
            "prompt_tokens": 3,
            "completion_tokens": 5,
            "total_tokens": 8,
            "cached_tokens": 2,
            "completion_tokens_details": {"reasoning_tokens": 1},
        },
    )


def test_openai_response_payload_maps_text_tools_and_usage() -> None:
    payload = openai_response_payload(
        _output(
            thinking="think",
            text="hello",
            calls=[{"name": "lookup", "args": {"q": "x"}, "id": "call_1"}],
        ),
        "gemini-3.7-flash",
    )

    choice = payload["choices"][0]
    message = choice["message"]
    assert message["content"] == "hello"
    assert message["reasoning_content"] == "think"
    assert message["tool_calls"][0]["id"] == "call_1"
    assert message["tool_calls"][0]["function"]["name"] == "lookup"
    assert json.loads(message["tool_calls"][0]["function"]["arguments"]) == {"q": "x"}
    assert choice["finish_reason"] == "tool_calls"
    assert payload["usage"]["prompt_tokens"] == 3
    assert payload["usage"]["completion_tokens"] == 5
    assert payload["usage"]["total_tokens"] == 8
    assert payload["usage"]["prompt_tokens_details"]["cached_tokens"] == 2


def test_openai_response_includes_thought_signature() -> None:
    payload = openai_response_payload(
        _output(
            calls=[
                {
                    "name": "lookup",
                    "args": {"q": "x"},
                    "id": "call_1",
                    "thought_signature": "sig",
                }
            ]
        ),
        "gemini-3.7-flash",
    )

    tool_call = payload["choices"][0]["message"]["tool_calls"][0]
    assert tool_call["extra_content"] == {
        "google": {"thought_signature": "sig"}
    }


def test_anthropic_response_payload_maps_text_tools_and_usage() -> None:
    payload = anthropic_response_payload(
        _output(
            thinking="think",
            text="hello",
            calls=[{"name": "lookup", "args": {"q": "x"}, "id": "toolu_1"}],
        ),
        "gemini-3.7-flash",
    )

    blocks = payload["content"]
    assert blocks[0]["type"] == "thinking"
    assert blocks[0]["thinking"] == "think"
    assert blocks[1] == {"type": "text", "text": "hello"}
    assert blocks[2]["type"] == "tool_use"
    assert blocks[2]["id"] == "toolu_1"
    assert blocks[2]["input"] == {"q": "x"}
    assert payload["stop_reason"] == "tool_use"
    assert payload["usage"]["input_tokens"] == 3
    assert payload["usage"]["output_tokens"] == 5
    assert payload["usage"]["cache_read_input_tokens"] == 2


def test_anthropic_response_includes_redacted_thinking_signature() -> None:
    payload = anthropic_response_payload(
        _output(
            calls=[
                {
                    "name": "lookup",
                    "args": {"q": "x"},
                    "id": "toolu_1",
                    "thought_signature": "sig",
                }
            ]
        ),
        "gemini-3.7-flash",
    )

    blocks = payload["content"]
    redacted = [block for block in blocks if block["type"] == "redacted_thinking"]
    assert redacted
    assert redacted[0]["data"] == "sig"


def test_anthropic_stream_encoder_emits_valid_event_sequence() -> None:
    encoder = AnthropicStreamEncoder(
        model="gemini-3.7-flash",
        message_id="msg_1",
        input_tokens=4,
    )
    events = encoder.start()
    events += encoder.feed("thinking", "hmm")
    events += encoder.feed("thought_signature", "sig")
    events += encoder.feed("body", "hello")
    events += encoder.feed(
        "tool_calls",
        [
            {
                "name": "lookup",
                "args": {"q": "x"},
                "id": "toolu_1",
                "thought_signature": "sig",
            }
        ],
    )
    events += encoder.finish(
        {"prompt_tokens": 4, "completion_tokens": 6, "total_tokens": 10}, 1
    )
    combined = "".join(events)

    assert "event: message_start" in combined
    assert "event: content_block_delta" in combined
    assert '"type": "thinking_delta"' in combined
    assert '"type": "signature_delta"' in combined
    assert '"type": "text_delta"' in combined
    assert "event: content_block_start" in combined
    assert '"type": "redacted_thinking"' in combined
    assert '"data": "sig"' in combined
    assert '"type": "tool_use"' in combined
    assert '"type": "input_json_delta"' in combined
    assert "event: message_delta" in combined
    assert '"stop_reason": "tool_use"' in combined
    assert "event: message_stop" in combined
    assert combined.count("event: content_block_stop") == 4
