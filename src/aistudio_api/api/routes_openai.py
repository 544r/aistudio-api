"""OpenAI-compatible Chat Completions routes."""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncGenerator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from aistudio_api.api.compat.convert import openai_to_gemini
from aistudio_api.api.compat.openai import (
    chunk_payload,
    finish_reason,
    image_url,
    response_payload,
    sse,
    sse_done,
    sse_error,
    tool_calls_payload,
    usage_payload,
)
from aistudio_api.api.dependencies import get_client, get_runtime_state
from aistudio_api.api.schemas import GeminiGenerateContentRequest
from aistudio_api.application.api_service_compat import generate_output, stream_events
from aistudio_api.application.api_service_gemini import classify_gemini_error_payload
from aistudio_api.infrastructure.gateway.client import AIStudioClient
from aistudio_api.infrastructure.gateway.model_discovery import model_discovery
from aistudio_api.infrastructure.utils.logger import get_logger

logger = get_logger("routes.openai")

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
            "error": {
                "message": message,
                "type": _error_type(status),
                "param": None,
                "code": status,
            }
        },
    )


def _model_name(payload: dict[str, object]) -> str:
    raw = payload.get("model")
    return raw if isinstance(raw, str) and raw else "gemini-3.7-flash"


def _include_usage(payload: dict[str, object]) -> bool:
    options = payload.get("stream_options")
    return isinstance(options, dict) and bool(options.get("include_usage"))


async def _chat_stream(
    payload: dict[str, object],
    req: GeminiGenerateContentRequest,
    model: str,
    client: AIStudioClient,
) -> AsyncGenerator[str, None]:
    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())
    include_usage = _include_usage(payload)
    yield sse(
        chunk_payload(
            completion_id=completion_id,
            created=created,
            model=model,
            delta={"role": "assistant", "content": ""},
        )
    )
    tool_index = 0
    final_finish: object = None
    final_usage: dict[str, object] | None = None
    try:
        async for event_type, value in stream_events(model, req, client):
            if event_type == "response_id":
                completion_id = f"chatcmpl-{value}"
                continue
            if event_type == "usage":
                final_usage = value if isinstance(value, dict) else None
                continue
            if event_type == "finish_reason":
                final_finish = value
                continue
            if event_type == "done":
                break
            if event_type == "thought_signature":
                continue
            if event_type == "body":
                delta: dict[str, object] = {"content": str(value)}
            elif event_type == "thinking":
                delta = {"reasoning_content": str(value)}
            elif event_type == "tool_calls":
                calls = [
                    call for call in (value if isinstance(value, list) else [])
                    if isinstance(call, dict)
                ]
                prepared = tool_calls_payload(calls)
                for call in prepared:
                    call["index"] = tool_index
                    tool_index += 1
                delta = {"tool_calls": prepared}
            elif event_type in ("images", "reasoning_images"):
                images = value if isinstance(value, list) else []
                delta = {
                    "images": [
                        {"type": "image_url", "image_url": {"url": image_url(image)}}
                        for image in images
                        if hasattr(image, "data")
                    ]
                }
            else:
                continue
            yield sse(
                chunk_payload(
                    completion_id=completion_id,
                    created=created,
                    model=model,
                    delta=delta,
                )
            )
    except Exception as exc:
        logger.warning("OpenAI 流式请求失败: %s", exc)
        yield sse_error(exc)
        yield sse_done()
        return

    yield sse(
        chunk_payload(
            completion_id=completion_id,
            created=created,
            model=model,
            delta={},
            finish=finish_reason(final_finish, has_tool_calls=tool_index > 0),
        )
    )
    if include_usage:
        yield sse(
            chunk_payload(
                completion_id=completion_id,
                created=created,
                model=model,
                include_choices=False,
                usage=usage_payload(final_usage),
            )
        )
    yield sse_done()


@router.post("/v1/chat/completions")
async def chat_completions(
    payload: dict[str, object],
    client: AIStudioClient = Depends(get_client),
):
    try:
        req = openai_to_gemini(payload)
        model = _model_name(payload)
        if bool(payload.get("stream")):
            logger.info("收到 OpenAI 流式请求: model=%s", model)
            return StreamingResponse(
                _chat_stream(payload, req, model, client),
                media_type="text/event-stream",
                headers=_SSE_HEADERS,
            )
        logger.info("收到 OpenAI 非流式请求: model=%s", model)
        _, output = await generate_output(model, req, client)
        return JSONResponse(response_payload(output, model))
    except Exception as exc:
        logger.warning("OpenAI 请求失败: %s", exc)
        return _error_response(exc)


@router.get("/v1/models")
async def list_models(
    request: Request,
    runtime_state=Depends(get_runtime_state),
):
    session = runtime_state.client._session if runtime_state.client else None
    discovered = await model_discovery.get_models(session=session)
    if request.headers.get("anthropic-version"):
        data: list[dict[str, object]] = []
        for item in discovered:
            model_id = str(item.get("id") or item.get("name") or "").removeprefix(
                "models/"
            )
            data.append(
                {
                    "type": "model",
                    "id": model_id,
                    "display_name": str(item.get("displayName") or model_id),
                    "created_at": "1970-01-01T00:00:00Z",
                }
            )
        first_id = data[0]["id"] if data else None
        last_id = data[-1]["id"] if data else None
        return {
            "data": data,
            "has_more": False,
            "first_id": first_id,
            "last_id": last_id,
        }
    data = []
    for item in discovered:
        model_id = str(item.get("id") or item.get("name") or "").removeprefix(
            "models/"
        )
        data.append(
            {
                "id": model_id,
                "object": "model",
                "created": 0,
                "owned_by": "google",
            }
        )
    return {"object": "list", "data": data}


@router.get("/v1/models/{model_id:path}")
async def get_model(
    model_id: str,
    request: Request,
    runtime_state=Depends(get_runtime_state),
):
    session = runtime_state.client._session if runtime_state.client else None
    model_info = await model_discovery.get_model_by_id(model_id, session=session)
    if model_info is None:
        return JSONResponse(
            status_code=404,
            content={
                "error": {
                    "message": f"Model '{model_id}' not found",
                    "type": "not_found_error",
                    "param": None,
                    "code": 404,
                }
            },
        )
    clean_id = str(model_info.get("id") or model_info.get("name") or "").removeprefix(
        "models/"
    )
    if request.headers.get("anthropic-version"):
        return {
            "type": "model",
            "id": clean_id,
            "display_name": str(model_info.get("displayName") or clean_id),
            "created_at": "1970-01-01T00:00:00Z",
        }
    return {
        "id": clean_id,
        "object": "model",
        "created": 0,
        "owned_by": "google",
    }


__all__ = ["router"]
