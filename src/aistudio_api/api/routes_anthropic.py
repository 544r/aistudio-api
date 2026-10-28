"""Anthropic Messages-compatible routes."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncGenerator

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, StreamingResponse

from aistudio_api.api.compat.anthropic import (
    AnthropicStreamEncoder,
    response_payload,
    sse_error,
)
from aistudio_api.api.compat.convert import anthropic_to_gemini
from aistudio_api.api.dependencies import get_client
from aistudio_api.api.schemas import GeminiGenerateContentRequest
from aistudio_api.application.api_service_compat import generate_output, stream_events
from aistudio_api.application.api_service_gemini import classify_gemini_error_payload
from aistudio_api.infrastructure.gateway.client import AIStudioClient
from aistudio_api.infrastructure.utils.logger import get_logger

logger = get_logger("routes.anthropic")

router = APIRouter()

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}


def _error_type(status: int) -> str:
    if status == 400:
        return "invalid_request_error"
    if status == 401:
        return "authentication_error"
    if status == 403:
        return "permission_error"
    if status == 404:
        return "not_found_error"
    if status == 429:
        return "rate_limit_error"
    return "api_error"


def _error_response(exc: Exception) -> JSONResponse:
    status, message, _ = classify_gemini_error_payload(exc)
    return JSONResponse(
        status_code=status,
        content={
            "type": "error",
            "error": {"type": _error_type(status), "message": message},
        },
    )


def _model_name(payload: dict[str, object]) -> str:
    raw = payload.get("model")
    return raw if isinstance(raw, str) and raw else "gemini-3.7-flash"


def _estimate_input_tokens(payload: dict[str, object]) -> int:
    text = json.dumps(
        {
            "messages": payload.get("messages"),
            "system": payload.get("system"),
            "tools": payload.get("tools"),
        },
        ensure_ascii=False,
        default=str,
    )
    return max(1, len(text) // 4)


async def _messages_stream(
    payload: dict[str, object],
    req: GeminiGenerateContentRequest,
    model: str,
    client: AIStudioClient,
) -> AsyncGenerator[str, None]:
    encoder = AnthropicStreamEncoder(
        model=model,
        message_id=f"msg_{uuid.uuid4().hex}",
        input_tokens=_estimate_input_tokens(payload),
    )
    for event in encoder.start():
        yield event
    final_usage: dict[str, object] | None = None
    final_finish: object = None
    try:
        async for event_type, value in stream_events(model, req, client):
            if event_type == "usage":
                final_usage = value if isinstance(value, dict) else None
                continue
            if event_type == "finish_reason":
                final_finish = value
                continue
            if event_type == "done":
                break
            if event_type == "response_id":
                continue
            for chunk in encoder.feed(event_type, value):
                yield chunk
    except Exception as exc:
        logger.warning("Anthropic 流式请求失败: %s", exc)
        yield sse_error(exc)
        return
    for event in encoder.finish(final_usage, final_finish):
        yield event


@router.post("/v1/messages")
async def messages(
    payload: dict[str, object],
    client: AIStudioClient = Depends(get_client),
):
    try:
        if not payload.get("max_tokens"):
            raise ValueError("max_tokens is required")
        req = anthropic_to_gemini(payload)
        model = _model_name(payload)
        if bool(payload.get("stream")):
            logger.info("收到 Anthropic 流式请求: model=%s", model)
            return StreamingResponse(
                _messages_stream(payload, req, model, client),
                media_type="text/event-stream",
                headers=_SSE_HEADERS,
            )
        logger.info("收到 Anthropic 非流式请求: model=%s", model)
        _, output = await generate_output(model, req, client)
        return JSONResponse(response_payload(output, model))
    except Exception as exc:
        logger.warning("Anthropic 请求失败: %s", exc)
        return _error_response(exc)


@router.post("/v1/messages/count_tokens")
async def count_tokens(payload: dict[str, object]):
    return {"input_tokens": _estimate_input_tokens(payload)}


__all__ = ["router"]
