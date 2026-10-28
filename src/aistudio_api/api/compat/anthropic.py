"""Anthropic Messages response and SSE encoding."""

from __future__ import annotations

import base64
import json
import uuid

from aistudio_api.application.api_service_gemini import to_gemini_finish_reason
from aistudio_api.domain.models import GeneratedImage, ModelOutput

_REFUSAL_REASONS = {
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
    cached = _int_value(data.get("cached_tokens") or data.get("cache_read_input_tokens"))
    return {
        "input_tokens": prompt,
        "output_tokens": completion,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": cached,
    }


def finish_reason(wire_code: object, *, has_tool_calls: bool = False) -> str:
    if has_tool_calls:
        return "tool_use"
    if not isinstance(wire_code, int):
        return "end_turn"
    gemini_reason = to_gemini_finish_reason(wire_code)
    if gemini_reason == "MAX_TOKENS":
        return "max_tokens"
    if gemini_reason in _REFUSAL_REASONS:
        return "refusal"
    return "end_turn"


def _call_id(value: object) -> str:
    text = str(value or "").strip()
    return text or f"toolu_{uuid.uuid4().hex[:16]}"


def _call_args(call: dict[str, object]) -> object:
    raw = call.get("args")
    if raw is None:
        raw = call.get("arguments")
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {"input": raw}
        return parsed if isinstance(parsed, (dict, list)) else {"input": raw}
    if raw is None:
        raw_value = call.get("raw")
        if isinstance(raw_value, list) and len(raw_value) > 1:
            raw = raw_value[1]
    return {} if raw is None else raw


def _image_source(image: GeneratedImage) -> dict[str, object]:
    return {
        "type": "base64",
        "media_type": image.mime,
        "data": base64.b64encode(image.data).decode("ascii"),
    }


def content_blocks(output: ModelOutput) -> list[dict[str, object]]:
    blocks: list[dict[str, object]] = []
    if output.thinking:
        signature = ""
        if output.candidates:
            signature = output.candidates[0].thought_signature
        blocks.append(
            {
                "type": "thinking",
                "thinking": output.thinking,
                "signature": signature or "",
            }
        )
    if output.text:
        blocks.append({"type": "text", "text": output.text})
    for call in output.function_calls:
        signature = str(call.get("thought_signature") or "")
        if signature:
            blocks.append({"type": "redacted_thinking", "data": signature})
        blocks.append(
            {
                "type": "tool_use",
                "id": _call_id(call.get("call_id") or call.get("id")),
                "name": str(call.get("name") or "tool"),
                "input": _call_args(call),
            }
        )
    if not blocks:
        blocks.append({"type": "text", "text": ""})
    return blocks


def response_payload(output: ModelOutput, model: str) -> dict[str, object]:
    candidate = output.candidates[0] if output.candidates else None
    calls = output.function_calls
    payload: dict[str, object] = {
        "id": output.response_id or f"msg_{uuid.uuid4().hex}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content_blocks(output),
        "stop_reason": finish_reason(
            candidate.finish_reason if candidate else None,
            has_tool_calls=bool(calls),
        ),
        "stop_sequence": None,
        "usage": usage_payload(output.usage),
    }
    if output.images:
        payload["images"] = [
            {"media_type": image.mime, "data": _image_source(image)["data"]}
            for image in output.images
        ]
    return payload


def sse(event: str, payload: dict[str, object]) -> str:
    return (
        f"event: {event}\n"
        "data: "
        + json.dumps(payload, ensure_ascii=False)
        + "\n\n"
    )


def sse_error(exc: Exception) -> str:
    from aistudio_api.application.api_service_gemini import (
        classify_gemini_error_payload,
    )

    code, message, _ = classify_gemini_error_payload(exc)
    if code == 400:
        error_type = "invalid_request_error"
    elif code == 401:
        error_type = "authentication_error"
    elif code == 403:
        error_type = "permission_error"
    elif code == 404:
        error_type = "not_found_error"
    elif code == 429:
        error_type = "rate_limit_error"
    else:
        error_type = "api_error"
    return sse(
        "error",
        {"type": "error", "error": {"type": error_type, "message": message}},
    )


class AnthropicStreamEncoder:
    """Translate raw upstream events into Anthropic Messages SSE events."""

    def __init__(self, *, model: str, message_id: str, input_tokens: int) -> None:
        self.model = model
        self.message_id = message_id
        self.input_tokens = input_tokens
        self.index = 0
        self.open_block: str | None = None
        self.saw_block = False
        self.saw_tool_call = False

    def start(self) -> list[str]:
        return [
            sse(
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": self.message_id,
                        "type": "message",
                        "role": "assistant",
                        "model": self.model,
                        "content": [],
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {
                            "input_tokens": self.input_tokens,
                            "output_tokens": 0,
                        },
                    },
                },
            )
        ]

    def _close_block(self) -> list[str]:
        if self.open_block is None:
            return []
        index = self.index
        self.index += 1
        self.open_block = None
        return [sse("content_block_stop", {"type": "content_block_stop", "index": index})]

    def _open(self, block_type: str, block: dict[str, object]) -> list[str]:
        index = self.index
        self.open_block = block_type
        self.saw_block = True
        return [
            sse(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": block,
                },
            )
        ]

    def _delta(self, delta: dict[str, object]) -> list[str]:
        return [
            sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": self.index,
                    "delta": delta,
                },
            )
        ]

    def feed(self, event_type: str, payload: object) -> list[str]:
        output: list[str] = []
        if event_type == "thinking":
            text = str(payload)
            if not text:
                return output
            if self.open_block != "thinking":
                output.extend(self._close_block())
                output.extend(self._open("thinking", {"type": "thinking", "thinking": ""}))
            output.extend(self._delta({"type": "thinking_delta", "thinking": text}))
            return output
        if event_type == "thought_signature":
            signature = str(payload)
            if self.open_block == "thinking" and signature:
                output.extend(self._delta({"type": "signature_delta", "signature": signature}))
                output.extend(self._close_block())
            return output
        if event_type == "body":
            text = str(payload)
            if not text:
                return output
            if self.open_block != "text":
                output.extend(self._close_block())
                output.extend(self._open("text", {"type": "text", "text": ""}))
            output.extend(self._delta({"type": "text_delta", "text": text}))
            return output
        if event_type == "tool_calls":
            calls = payload if isinstance(payload, list) else []
            output.extend(self._close_block())
            for call in calls:
                if not isinstance(call, dict):
                    continue
                self.saw_tool_call = True
                signature = str(call.get("thought_signature") or "")
                if signature:
                    output.extend(
                        self._open(
                            "redacted_thinking",
                            {"type": "redacted_thinking", "data": signature},
                        )
                    )
                    output.extend(self._close_block())
                call_id = _call_id(call.get("call_id") or call.get("id"))
                name = str(call.get("name") or "tool")
                output.extend(
                    self._open(
                        "tool",
                        {
                            "type": "tool_use",
                            "id": call_id,
                            "name": name,
                            "input": {},
                        },
                    )
                )
                arguments = json.dumps(
                    _call_args(call), ensure_ascii=False, separators=(",", ":")
                )
                output.extend(
                    self._delta({"type": "input_json_delta", "partial_json": arguments})
                )
                output.extend(self._close_block())
            return output
        return output

    def finish(self, usage: dict[str, object] | None, wire_finish: object) -> list[str]:
        output: list[str] = []
        if self.open_block == "thinking":
            output.extend(self._close_block())
        output.extend(self._close_block())
        if not self.saw_block:
            output.extend(self._open("text", {"type": "text", "text": ""}))
            output.extend(self._close_block())
        usage_data = usage_payload(usage)
        output.append(
            sse(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {
                        "stop_reason": finish_reason(
                            wire_finish, has_tool_calls=self.saw_tool_call
                        ),
                        "stop_sequence": None,
                    },
                    "usage": {
                        "output_tokens": usage_data["output_tokens"],
                    },
                },
            )
        )
        output.append(sse("message_stop", {"type": "message_stop"}))
        return output


__all__ = [
    "AnthropicStreamEncoder",
    "content_blocks",
    "finish_reason",
    "response_payload",
    "sse",
    "sse_error",
    "usage_payload",
]
