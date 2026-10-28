"""Convert OpenAI and Anthropic requests into the native Gemini request model."""

from __future__ import annotations

import base64
import json
from contextlib import suppress
from urllib.parse import unquote

from aistudio_api.api.schemas import (
    GeminiContent,
    GeminiFunctionCall,
    GeminiFunctionResponse,
    GeminiGenerateContentRequest,
    GeminiGenerationConfig,
    GeminiInlineData,
    GeminiPart,
    GeminiTool,
)
from aistudio_api.application import thought_signatures


def _as_dict(value: object) -> dict[str, object] | None:
    return value if isinstance(value, dict) else None


def _as_list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _as_text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = int(value)
    return number if number > 0 else None


def _float_or_none(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _append_content(
    contents: list[GeminiContent], role: str, parts: list[GeminiPart]
) -> None:
    if not parts:
        return
    if contents and contents[-1].role == role:
        contents[-1].parts.extend(parts)
        return
    contents.append(GeminiContent(role=role, parts=parts))


def _data_url_image(url: str) -> GeminiInlineData:
    header, separator, data = url.partition(",")
    if not separator or not header.startswith("data:"):
        raise ValueError(
            "Only data: URLs are supported for inline images; remote image URLs "
            "must be downloaded by the client and sent as base64"
        )
    metadata = header[5:]
    fields = metadata.split(";")
    mime = fields[0] or "application/octet-stream"
    if "base64" in fields[1:]:
        encoded = data
    else:
        encoded = base64.b64encode(unquote(data).encode("utf-8")).decode("ascii")
    return GeminiInlineData(mimeType=mime, data=encoded)


def _inline_from_anthropic_source(source: object) -> GeminiInlineData:
    obj = _as_dict(source)
    if obj is None:
        raise ValueError("image source is required")
    source_type = str(obj.get("type") or "")
    if source_type == "base64":
        data = obj.get("data")
        mime = obj.get("media_type")
        if not isinstance(data, str) or not isinstance(mime, str):
            raise ValueError("base64 image source requires media_type and data")
        return GeminiInlineData(mimeType=mime, data=data)
    if source_type == "url":
        url = obj.get("url")
        if isinstance(url, str):
            return _data_url_image(url)
    raise ValueError(
        "Only base64 image sources and data: URLs are supported; remote URLs "
        "must be downloaded by the client and sent as base64"
    )


def _tool_result_payload(value: object) -> object:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {"result": ""}
        with suppress(json.JSONDecodeError):
            parsed = json.loads(text)
            if isinstance(parsed, (dict, list)):
                return parsed
        return {"result": value}
    if isinstance(value, (dict, list)):
        return value
    return {"result": value}


def _function_args(value: object) -> object:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        with suppress(json.JSONDecodeError):
            parsed = json.loads(text)
            if isinstance(parsed, (dict, list)):
                return parsed
        return {"input": value}
    if isinstance(value, (dict, list)):
        return value
    if value is None:
        return {}
    return {"input": value}


def _extra_signature(call: dict[str, object]) -> str:
    extra = _as_dict(call.get("extra_content"))
    if extra is None:
        return ""
    google = _as_dict(extra.get("google"))
    if google is None:
        return ""
    signature = google.get("thought_signature") or google.get("thoughtSignature")
    return signature if isinstance(signature, str) else ""


def _openai_content_parts(content: object) -> list[GeminiPart]:
    if isinstance(content, str):
        return [GeminiPart(text=content)] if content else []
    parts: list[GeminiPart] = []
    for item in _as_list(content):
        if isinstance(item, str):
            if item:
                parts.append(GeminiPart(text=item))
            continue
        obj = _as_dict(item)
        if obj is None:
            continue
        part_type = str(obj.get("type") or "")
        if part_type in ("text", "input_text"):
            text = obj.get("text")
            if isinstance(text, str) and text:
                parts.append(GeminiPart(text=text))
            continue
        if part_type in ("image_url", "input_image"):
            raw_url = obj.get("image_url") or obj.get("url")
            if isinstance(raw_url, dict):
                raw_url = raw_url.get("url")
            if isinstance(raw_url, str):
                parts.append(GeminiPart(inlineData=_data_url_image(raw_url)))
            continue
        if part_type == "input_audio":
            raise ValueError("input_audio content is not supported")
    return parts


def _openai_tools(value: object) -> list[GeminiTool] | None:
    declarations: list[dict[str, object]] = []
    for item in _as_list(value):
        obj = _as_dict(item)
        if obj is None:
            continue
        tool_type = obj.get("type")
        if tool_type not in (None, "function"):
            continue
        function = _as_dict(obj.get("function"))
        if function is None:
            continue
        name = function.get("name")
        if not isinstance(name, str) or not name:
            continue
        declaration: dict[str, object] = {"name": name}
        description = function.get("description")
        if isinstance(description, str) and description:
            declaration["description"] = description
        parameters = function.get("parameters")
        if isinstance(parameters, dict):
            declaration["parameters"] = parameters
        declarations.append(declaration)
    if not declarations:
        return None
    return [GeminiTool(functionDeclarations=declarations)]


def _openai_tool_config(value: object) -> dict[str, object] | None:
    if isinstance(value, str):
        mode = {"none": "NONE", "auto": "AUTO", "required": "ANY"}.get(value)
        if mode is None:
            return None
        return {"functionCallingConfig": {"mode": mode}}
    obj = _as_dict(value)
    if obj is None:
        return None
    if obj.get("type") == "function":
        function = _as_dict(obj.get("function")) or {}
        name = function.get("name")
        if isinstance(name, str) and name:
            return {
                "functionCallingConfig": {
                    "mode": "ANY",
                    "allowedFunctionNames": [name],
                }
            }
    return None


def _google_thinking_config(
    payload: dict[str, object],
) -> list[object] | dict[str, object] | None:
    for key in ("extra_body", "google"):
        container = _as_dict(payload.get(key))
        if container is None:
            continue
        config = container.get("thinking_config") or container.get("thinkingConfig")
        if isinstance(config, dict):
            return {str(name): value for name, value in config.items()}
        if isinstance(config, list):
            return list(config)
    return None


def _openai_generation_config(
    payload: dict[str, object],
) -> GeminiGenerationConfig | None:
    temperature = _float_or_none(payload.get("temperature"))
    top_p = _float_or_none(payload.get("top_p"))
    max_tokens = _positive_int(
        payload.get("max_completion_tokens") or payload.get("max_tokens")
    )
    presence = _float_or_none(payload.get("presence_penalty"))
    frequency = _float_or_none(payload.get("frequency_penalty"))
    seed: int | None = None
    raw_seed = payload.get("seed")
    if isinstance(raw_seed, int) and not isinstance(raw_seed, bool):
        seed = raw_seed
    stop_sequences: list[str] | None = None
    stop = payload.get("stop")
    if isinstance(stop, str) and stop:
        stop_sequences = [stop]
    elif isinstance(stop, list):
        sequences = [item for item in stop if isinstance(item, str) and item]
        if sequences:
            stop_sequences = sequences
    response_mime_type: str | None = None
    response_schema: list[object] | dict[str, object] | None = None
    response_format = _as_dict(payload.get("response_format"))
    if response_format is not None:
        format_type = response_format.get("type")
        if format_type == "json_object":
            response_mime_type = "application/json"
        elif format_type == "json_schema":
            json_schema = _as_dict(response_format.get("json_schema")) or {}
            schema = json_schema.get("schema")
            if isinstance(schema, dict):
                response_mime_type = "application/json"
                response_schema = {str(key): value for key, value in schema.items()}
    thinking_config = _google_thinking_config(payload)
    if thinking_config is None:
        effort = payload.get("reasoning_effort")
        if isinstance(effort, str) and effort.strip():
            level = effort.strip().upper()
            if level in ("MINIMAL", "LOW", "MEDIUM", "HIGH"):
                thinking_config = {"level": level, "mode": 1}
    if all(
        value is None
        for value in (
            stop_sequences,
            temperature,
            top_p,
            max_tokens,
            presence,
            frequency,
            seed,
            response_mime_type,
            response_schema,
            thinking_config,
        )
    ):
        return None
    return GeminiGenerationConfig(
        stopSequences=stop_sequences,
        temperature=temperature,
        topP=top_p,
        maxOutputTokens=max_tokens,
        presencePenalty=presence,
        frequencyPenalty=frequency,
        responseMimeType=response_mime_type,
        responseSchema=response_schema,
        seed=seed,
        thinkingConfig=thinking_config,
    )


def openai_to_gemini(payload: dict[str, object]) -> GeminiGenerateContentRequest:
    messages = payload.get("messages")
    if not isinstance(messages, list):
        raise ValueError("messages must be an array")

    contents: list[GeminiContent] = []
    system_texts: list[str] = []
    tool_names: dict[str, str] = {}
    seen_non_system = False

    for raw_message in messages:
        message = _as_dict(raw_message)
        if message is None:
            continue
        role = str(message.get("role") or "user")
        if role not in ("system", "developer"):
            seen_non_system = True
        if role in ("system", "developer"):
            text = _openai_system_text(message.get("content"))
            if text:
                if seen_non_system:
                    # A system message after the conversation has started is usually a
                    # mid-conversation control prompt (anti-truncation, RP directives).
                    # Keep it at its original position instead of hoisting it to the top.
                    _append_content(contents, "user", [GeminiPart(text=text)])
                else:
                    system_texts.append(text)
            continue
        if role in ("tool", "function"):
            call_id = str(message.get("tool_call_id") or message.get("id") or "")
            name = tool_names.get(call_id) or str(message.get("name") or "tool")
            response = _tool_result_payload(message.get("content"))
            _append_content(
                contents,
                "user",
                [
                    GeminiPart(
                        functionResponse=GeminiFunctionResponse(
                            name=name,
                            response=response,
                            id=call_id or None,
                        )
                    )
                ],
            )
            continue

        parts = _openai_content_parts(message.get("content"))
        if role == "assistant":
            reasoning = message.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning:
                parts.insert(0, GeminiPart(text=reasoning, thought=True))
            for raw_call in _as_list(message.get("tool_calls")):
                call = _as_dict(raw_call)
                if call is None:
                    continue
                function = _as_dict(call.get("function")) or {}
                name = str(function.get("name") or "tool")
                call_id = str(call.get("id") or "")
                if call_id:
                    tool_names[call_id] = name
                args = _function_args(function.get("arguments"))
                signature = _extra_signature(call) or thought_signatures.lookup(
                    call_id, name, args
                )
                if signature:
                    thought_signatures.remember_one(call_id, name, args, signature)
                parts.append(
                    GeminiPart(
                        functionCall=GeminiFunctionCall(
                            name=name,
                            args=args,
                            id=call_id or None,
                        ),
                        thoughtSignature=signature or None,
                    )
                )
            legacy_call = _as_dict(message.get("function_call"))
            if legacy_call is not None:
                name = str(legacy_call.get("name") or "tool")
                parts.append(
                    GeminiPart(
                        functionCall=GeminiFunctionCall(
                            name=name,
                            args=_function_args(legacy_call.get("arguments")),
                        )
                    )
                )
            _append_content(contents, "model", parts)
        else:
            _append_content(contents, "user", parts)

    if not contents:
        raise ValueError("messages must contain at least one non-system message")

    system_instruction = None
    if system_texts:
        system_instruction = GeminiContent(
            role="user",
            parts=[GeminiPart(text="\n\n".join(system_texts))],
        )

    return GeminiGenerateContentRequest(
        contents=contents,
        systemInstruction=system_instruction,
        tools=_openai_tools(payload.get("tools")),
        toolConfig=_openai_tool_config(payload.get("tool_choice")),
        generationConfig=_openai_generation_config(payload),
    )


def _openai_system_text(content: object) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(
        str(part.get("text") or "")
        for part in _as_list(content)
        if isinstance(part, dict) and part.get("type") in ("text", "input_text")
    )


def _anthropic_system_text(value: object) -> str:
    if isinstance(value, str):
        return value
    return "\n".join(
        str(block.get("text") or "")
        for block in _as_list(value)
        if isinstance(block, dict) and block.get("type") == "text"
    )


def _anthropic_content_parts(content: object) -> list[GeminiPart]:
    if isinstance(content, str):
        return [GeminiPart(text=content)] if content else []
    parts: list[GeminiPart] = []
    pending_signature = ""
    for raw_block in _as_list(content):
        block = _as_dict(raw_block)
        if block is None:
            continue
        block_type = str(block.get("type") or "")
        if block_type == "redacted_thinking":
            data = block.get("data")
            if isinstance(data, str) and data:
                pending_signature = data
            continue
        if block_type == "text":
            text = block.get("text")
            if isinstance(text, str) and text:
                parts.append(GeminiPart(text=text))
            continue
        if block_type == "image":
            parts.append(
                GeminiPart(inlineData=_inline_from_anthropic_source(block.get("source")))
            )
            continue
        if block_type == "tool_use":
            name = str(block.get("name") or "tool")
            call_id = str(block.get("id") or "")
            args = _function_args(block.get("input"))
            signature = pending_signature or thought_signatures.lookup(
                call_id, name, args
            )
            pending_signature = ""
            if signature:
                thought_signatures.remember_one(call_id, name, args, signature)
            parts.append(
                GeminiPart(
                    functionCall=GeminiFunctionCall(
                        name=name,
                        args=args,
                        id=call_id or None,
                    ),
                    thoughtSignature=signature or None,
                )
            )
            continue
        if block_type == "tool_result":
            parts.extend(_anthropic_tool_result_parts(block))
            continue
        if block_type == "thinking":
            thinking = block.get("thinking")
            signature = block.get("signature")
            if isinstance(thinking, str) and thinking:
                parts.append(
                    GeminiPart(
                        text=thinking,
                        thought=True,
                        thoughtSignature=signature if isinstance(signature, str) else None,
                    )
                )
    return parts


def _anthropic_tool_result_parts(block: dict[str, object]) -> list[GeminiPart]:
    call_id = str(block.get("tool_use_id") or block.get("id") or "")
    name = str(block.get("name") or "tool")
    raw_content = block.get("content")
    text_chunks: list[str] = []
    extra_parts: list[GeminiPart] = []
    if isinstance(raw_content, str):
        text_chunks.append(raw_content)
    else:
        for raw_item in _as_list(raw_content):
            item = _as_dict(raw_item)
            if item is None:
                continue
            item_type = str(item.get("type") or "")
            if item_type == "text":
                text = item.get("text")
                if isinstance(text, str):
                    text_chunks.append(text)
            elif item_type == "image":
                extra_parts.append(
                    GeminiPart(
                        inlineData=_inline_from_anthropic_source(item.get("source"))
                    )
                )
    payload: object = {"result": "\n".join(text_chunks)} if text_chunks else {}
    return [
        GeminiPart(
            functionResponse=GeminiFunctionResponse(
                name=name,
                response=payload,
                id=call_id or None,
                parts=extra_parts or None,
            )
        )
    ]


def _anthropic_tools(value: object) -> list[GeminiTool] | None:
    declarations: list[dict[str, object]] = []
    for raw_tool in _as_list(value):
        tool = _as_dict(raw_tool)
        if tool is None:
            continue
        name = tool.get("name")
        if not isinstance(name, str) or not name:
            continue
        declaration: dict[str, object] = {"name": name}
        description = tool.get("description")
        if isinstance(description, str) and description:
            declaration["description"] = description
        schema = tool.get("input_schema")
        if isinstance(schema, dict):
            declaration["parameters"] = schema
        declarations.append(declaration)
    if not declarations:
        return None
    return [GeminiTool(functionDeclarations=declarations)]


def _anthropic_tool_config(value: object) -> dict[str, object] | None:
    tool_choice = _as_dict(value)
    if tool_choice is None:
        return None
    choice_type = str(tool_choice.get("type") or "")
    if choice_type == "auto":
        return {"functionCallingConfig": {"mode": "AUTO"}}
    if choice_type == "any":
        return {"functionCallingConfig": {"mode": "ANY"}}
    if choice_type == "none":
        return {"functionCallingConfig": {"mode": "NONE"}}
    if choice_type == "tool":
        name = tool_choice.get("name")
        if isinstance(name, str) and name:
            return {
                "functionCallingConfig": {
                    "mode": "ANY",
                    "allowedFunctionNames": [name],
                }
            }
    return None


def _anthropic_generation_config(
    payload: dict[str, object],
) -> GeminiGenerationConfig | None:
    max_tokens = _positive_int(payload.get("max_tokens"))
    temperature = _float_or_none(payload.get("temperature"))
    top_p = _float_or_none(payload.get("top_p"))
    top_k = _positive_int(payload.get("top_k"))
    stop_sequences: list[str] | None = None
    raw_stop_sequences = payload.get("stop_sequences")
    if isinstance(raw_stop_sequences, list):
        sequences = [
            item for item in raw_stop_sequences if isinstance(item, str) and item
        ]
        if sequences:
            stop_sequences = sequences

    thinking_config: list[object] | dict[str, object] | None = None
    thinking = _as_dict(payload.get("thinking"))
    if thinking is not None and thinking.get("type") == "enabled":
        budget = _positive_int(thinking.get("budget_tokens"))
        if budget is None:
            level = "HIGH"
        elif budget <= 1024:
            level = "LOW"
        elif budget <= 4096:
            level = "MEDIUM"
        else:
            level = "HIGH"
        thinking_config = {"level": level, "mode": 1}
    if all(
        value is None
        for value in (
            max_tokens,
            temperature,
            top_p,
            top_k,
            stop_sequences,
            thinking_config,
        )
    ):
        return None
    return GeminiGenerationConfig(
        maxOutputTokens=max_tokens,
        temperature=temperature,
        topP=top_p,
        topK=top_k,
        stopSequences=stop_sequences,
        thinkingConfig=thinking_config,
    )


def anthropic_to_gemini(payload: dict[str, object]) -> GeminiGenerateContentRequest:
    messages = payload.get("messages")
    if not isinstance(messages, list):
        raise ValueError("messages must be an array")

    contents: list[GeminiContent] = []
    tool_names: dict[str, str] = {}
    for raw_message in messages:
        message = _as_dict(raw_message)
        if message is None:
            continue
        role = str(message.get("role") or "user")
        parts = _anthropic_content_parts(message.get("content"))
        for part in parts:
            if part.functionCall is not None and part.functionCall.id:
                tool_names[part.functionCall.id] = part.functionCall.name
        for part in parts:
            if part.functionResponse is not None and part.functionResponse.id:
                part.functionResponse.name = tool_names.get(
                    part.functionResponse.id, part.functionResponse.name
                )
        _append_content(contents, "model" if role == "assistant" else "user", parts)

    if not contents:
        raise ValueError("messages must contain at least one message")

    system_text = _anthropic_system_text(payload.get("system"))
    system_instruction = (
        GeminiContent(role="user", parts=[GeminiPart(text=system_text)])
        if system_text
        else None
    )

    return GeminiGenerateContentRequest(
        contents=contents,
        systemInstruction=system_instruction,
        tools=_anthropic_tools(payload.get("tools")),
        toolConfig=_anthropic_tool_config(payload.get("tool_choice")),
        generationConfig=_anthropic_generation_config(payload),
    )


__all__ = ["anthropic_to_gemini", "openai_to_gemini"]
