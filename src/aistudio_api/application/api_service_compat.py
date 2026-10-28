"""Shared execution helpers for non-Gemini compatibility endpoints."""

from __future__ import annotations

from collections.abc import AsyncGenerator

from fastapi import HTTPException

from aistudio_api.api.schemas import GeminiGenerateContentRequest
from aistudio_api.api.state import runtime_state
from aistudio_api.application import thought_signatures
from aistudio_api.application.account_orchestrator import (
    MAX_RETRIES,
    ensure_active_account,
    record_rotator_event,
)
from aistudio_api.application.api_service_gemini import (
    handle_attempt_exception,
    to_gemini_finish_reason,
)
from aistudio_api.application.chat_service import (
    NormalizedGeminiRequest,
    normalize_gemini_request,
)
from aistudio_api.domain.models import ModelOutput
from aistudio_api.infrastructure.gateway.client import AIStudioClient
from aistudio_api.infrastructure.utils.logger import get_logger

logger = get_logger("api.compat")


def _quota_exhausted(model_path: str) -> HTTPException:
    return HTTPException(
        429,
        detail={
            "message": (
                f"All accounts reached daily quota for model '{model_path}'. "
                "Quota resets at 00:00 PST."
            ),
            "type": "rate_limit_exceeded",
        },
    )


async def generate_output(
    model_path: str,
    req: GeminiGenerateContentRequest,
    client: AIStudioClient,
) -> tuple[NormalizedGeminiRequest, ModelOutput]:
    """Run one non-stream generation with the same retry/failover policy as Gemini."""
    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES):
        await ensure_active_account(attempt, model=model_path)
        normalized: NormalizedGeminiRequest | None = None
        try:
            normalized = normalize_gemini_request(
                req, model_path, use_default_tools=False
            )
            logger.info(
                "兼容层生成请求: model=%s, 对话轮数=%s, 尝试第 %d 次",
                normalized.model,
                len(req.contents),
                attempt + 1,
            )
            output = await client.generate_content(
                model=normalized.model,
                capture_prompt=normalized.capture_prompt,
                capture_images=normalized.capture_images,
                contents=normalized.contents,
                system_instruction_content=normalized.system_instruction,
                tools=normalized.tools,
                tool_config=normalized.tool_config,
                safety_settings=normalized.safety_settings,
                temperature=normalized.temperature,
                top_p=normalized.top_p,
                top_k=normalized.top_k,
                max_tokens=normalized.max_tokens,
                generation_config_overrides=normalized.generation_config_overrides,
                sanitize_plain_text=False,
            )
            candidate = output.candidates[0] if output.candidates else None
            final_finish = to_gemini_finish_reason(
                candidate.finish_reason if candidate else None
            )
            thought_signatures.remember(output.function_calls)
            record_rotator_event(
                "success", model=normalized.model, usage=output.usage
            )
            runtime_state.record(
                normalized.model,
                "success",
                usage=output.usage,
                finish_reason=final_finish,
            )
            return normalized, output
        except Exception as exc:
            last_error = exc
            if await handle_attempt_exception(
                exc,
                attempt=attempt,
                model_path=model_path,
                normalized_model=normalized.model if normalized else None,
                client=client,
            ):
                continue
            raise
    raise _quota_exhausted(model_path) from last_error


async def stream_events(
    model_path: str,
    req: GeminiGenerateContentRequest,
    client: AIStudioClient,
) -> AsyncGenerator[tuple[str, object], None]:
    """Run one streaming generation and yield raw upstream events.

    Event types follow the browser client contract: body, thinking, tool_calls,
    images, reasoning_images, response_id, thought_signature, usage,
    finish_reason and done.
    """
    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES):
        has_yielded = False
        normalized: NormalizedGeminiRequest | None = None
        final_usage: dict[str, object] | None = None
        final_finish: object = None
        await ensure_active_account(attempt, model=model_path)
        try:
            normalized = normalize_gemini_request(
                req, model_path, use_default_tools=False
            )
            async for event_type, payload in client.stream_generate_content(
                model=normalized.model,
                capture_prompt=normalized.capture_prompt,
                capture_images=normalized.capture_images,
                contents=normalized.contents,
                system_instruction_content=normalized.system_instruction,
                tools=normalized.tools,
                tool_config=normalized.tool_config,
                safety_settings=normalized.safety_settings,
                temperature=normalized.temperature,
                top_p=normalized.top_p,
                top_k=normalized.top_k,
                max_tokens=normalized.max_tokens,
                generation_config_overrides=normalized.generation_config_overrides,
                sanitize_plain_text=False,
                force_refresh_capture=attempt > 0,
            ):
                if event_type == "usage":
                    final_usage = payload if isinstance(payload, dict) else None
                elif event_type == "finish_reason":
                    final_finish = payload
                elif event_type == "response_id":
                    yield ("response_id", str(payload))
                elif event_type == "thought_signature":
                    yield ("thought_signature", str(payload))
                elif event_type == "done":
                    break
                else:
                    if event_type == "tool_calls":
                        thought_signatures.remember(
                            [
                                call
                                for call in (payload if isinstance(payload, list) else [])
                                if isinstance(call, dict)
                            ]
                        )
                    has_yielded = True
                    yield (event_type, payload)
            final_finish_reason = (
                to_gemini_finish_reason(final_finish)
                if isinstance(final_finish, int)
                else "STOP"
            )
            record_rotator_event(
                "success", model=normalized.model, usage=final_usage
            )
            runtime_state.record(
                normalized.model,
                "success",
                usage=final_usage,
                finish_reason=final_finish_reason,
            )
            yield ("usage", final_usage)
            yield ("finish_reason", final_finish)
            yield ("done", None)
            return
        except Exception as exc:
            last_error = exc
            if await handle_attempt_exception(
                exc,
                attempt=attempt,
                model_path=model_path,
                normalized_model=normalized.model if normalized else None,
                client=client,
                has_yielded_data=has_yielded,
            ):
                continue
            raise
    raise _quota_exhausted(model_path) from last_error


__all__ = ["generate_output", "stream_events"]
