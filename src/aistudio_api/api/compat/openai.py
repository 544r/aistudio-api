"""OpenAI Chat Completions response and SSE encoding."""

from __future__ import annotations

import base64
import json
import time
import uuid
from contextlib import suppress

from aistudio_api.application.api_service_gemini import to_gemini_finish_reason
from aistudio_api.domain.models import GeneratedImage, ModelOutput

_CONTENT_FILTER_REASONS = {
    "SAFETY",
    "RECITATION",
    "BLOCKLIST",
    "PROHIBITED_CONTENT",
    "SPII",
    "IMAGE_SAFETY",
    "IMAGE_PROHIBITED_CONTENT",
    "IMAGE_RECITATION",
}


def _int_value(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return 0


def usage_payload(usage: dict[str, object] | None) -> dict[str, object]:
    data = usage or {}
    prompt = _int_value(data.get("prompt_tokens"))
    completion = _int_value(data.get("completion_tokens"))
    total = _int_value(data.get("total_tokens"))
    if total == 0 and (prompt or completion):
        total = prompt + completion
    details = data.get("completion_tokens_details")
    details_dict = details if isinstance(details, dict) else {}
    cached = _int_value(data.get("cached_tokens") or data.get("cache_read_input_tokens"))
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "prompt_tokens_details": {"cached_tokens": cached},
        "completion_tokens_details": {
            "reasoning_tokens": _int_value(details_dict.get("reasoning_tokens"))
        },
    }


def finish_reason(wire_code: object, *, has_tool_calls: bool = False) -> str:
    if has_tool_calls:
        return "tool_calls"
    if not isinstance(wire_code, int):
        return "stop"
    gemini_reason = to_gemini_finish_reason(wire_code)
    if gemini_reason == "MAX_TOKENS":
        return "length"
    if gemini_reason in _CONTENT_FILTER_REASONS:
        return "content_filter"
    return "stop"


def _call_id(value: object) -> str:
    text = str(value or "").strip()
    return text or f"call_{uuid.uuid4().hex[:16]}"


def _call_args(call: dict[str, object]) -> object:
    raw = call.get("args")
    if raw is None:
        raw = call.get("arguments")
    if isinstance(raw, str):
        with suppress(json.JSONDecodeError):
            parsed = json.loads(raw)
            if isinstance(parsed, (dict, list)):
                return parsed
        return {"input": raw}
    if raw is None:
        raw_value = call.get("raw")
        if isinstance(raw_value, list) and len(raw_value) > 1:
            raw = raw_value[1]
    return {} if raw is None else raw


def tool_calls_payload(
    function_calls: list[dict[str, object]],
) -> list[dict[str, object]]:
    payload: list[dict[str, object]] = []
    for call in function_calls:
        item: dict[str, object] = {
            "id": _call_id(call.get("call_id") or call.get("id")),
            "type": "function",
            "function": {
                "name": str(call.get("name") or "tool"),
                "arguments": json.dumps(
                    _call_args(call), ensure_ascii=False, separators=(",", ":")
                ),
            },
        }
        signature = str(call.get("thought_signature") or "")
        if signature:
            item["extra_content"] = {"google": {"thought_signature": signature}}
        payload.append(item)
    return payload


def image_url(image: GeneratedImage) -> str:
    encoded = base64.b64encode(image.data).decode("ascii")
    return f"data:{image.mime};base64,{encoded}"


def _images_payload(images: list[GeneratedImage]) -> list[dict[str, object]]:
    return [
        {"type": "image_url", "image_url": {"url": image_url(image)}}
        for image in images
    ]


def response_payload(output: ModelOutput, model: str) -> dict[str, object]:
    candidate = output.candidates[0] if output.candidates else None
    calls = tool_calls_payload(output.function_calls)
    message: dict[str, object] = {
        "role": "assistant",
        "content": output.text or None,
    }
    if output.thinking:
        message["reasoning_content"] = output.thinking
    if calls:
        message["tool_calls"] = calls
    if output.images:
        message["images"] = _images_payload(output.images)
    return {
        "id": output.response_id or f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason(
                    candidate.finish_reason if candidate else None,
                    has_tool_calls=bool(calls),
                ),
            }
        ],
        "usage": usage_payload(output.usage),
    }


def chunk_payload(
    *,
    completion_id: str,
    created: int,
    model: str,
    delta: dict[str, object] | None = None,
    finish: str | None = None,
    usage: dict[str, object] | None = None,
    include_choices: bool = True,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
    }
    if include_choices:
        payload["choices"] = [
            {
                "index": 0,
                "delta": delta or {},
                "finish_reason": finish,
            }
        ]
    else:
        payload["choices"] = []
    if usage is not None:
        payload["usage"] = usage
    return payload


def sse(payload: dict[str, object]) -> str:
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def sse_done() -> str:
    return "data: [DONE]\n\n"


def sse_error(exc: Exception) -> str:
    from aistudio_api.application.api_service_gemini import (
        classify_gemini_error_payload,
    )

    code, message, _ = classify_gemini_error_payload(exc)
    payload = {
        "error": {
            "message": message,
            "type": "invalid_request_error" if code < 500 else "api_error",
            "param": None,
            "code": code,
        }
    }
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


__all__ = [
    "chunk_payload",
    "finish_reason",
    "response_payload",
    "sse",
    "sse_done",
    "sse_error",
    "tool_calls_payload",
    "usage_payload",
]
