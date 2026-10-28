"""In-process store for Gemini tool-call thought signatures.

OpenAI and Anthropic clients do not have a native field for Gemini's
``thoughtSignature`` on function calls. The store remembers signatures from
upstream responses and restores them when the same tool call appears in a
later request history, keyed by call ID, function name and canonical arguments.
"""

from __future__ import annotations

import json
import threading

_CAPACITY = 4096

_lock = threading.Lock()
_signatures: dict[str, str] = {}
_order: list[str] = []


def _canonical_args(value: object) -> str:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ""
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            return text
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    except (TypeError, ValueError):
        return str(value)


def _key(call_id: str, name: str, args: object) -> str:
    return f"{call_id}\x00{name}\x00{_canonical_args(args)}"


def remember(calls: list[dict[str, object]]) -> None:
    """Remember every tool call that carries both an ID and a signature."""
    with _lock:
        for call in calls:
            call_id = str(call.get("id") or call.get("call_id") or "")
            name = str(call.get("name") or "")
            signature = str(call.get("thought_signature") or "")
            if not call_id or not name or not signature:
                continue
            args = call.get("args")
            if args is None:
                args = call.get("arguments")
            key = _key(call_id, name, args)
            if key not in _signatures:
                _order.append(key)
            _signatures[key] = signature
        while len(_order) > _CAPACITY:
            oldest = _order.pop(0)
            _signatures.pop(oldest, None)


def remember_one(
    call_id: str,
    name: str,
    args: object,
    signature: str,
) -> None:
    if not call_id or not name or not signature:
        return
    remember(
        [
            {
                "id": call_id,
                "name": name,
                "args": args,
                "thought_signature": signature,
            }
        ]
    )


def lookup(call_id: str, name: str, args: object) -> str:
    """Return the remembered signature for an identical tool call, if any."""
    if not call_id or not name:
        return ""
    with _lock:
        return _signatures.get(_key(call_id, name, args), "")


def reset() -> None:
    """Clear the store (used by tests and account resets)."""
    with _lock:
        _signatures.clear()
        _order.clear()


__all__ = ["lookup", "remember", "remember_one", "reset"]
