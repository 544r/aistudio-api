"""服务端 Cookie 代次推进流水（进程内、有界、不含敏感值）。

续命机制全部落地之后，"Cookie 有没有在自动更新"成了运营期最关心的问题：
在途响应捕获、探活回写、RotateCookies 抢救、浏览器 60s 回写都在推进代次，
但它们只打日志，前端完全不可见。这里把每一次推进记为结构化事件（谁、何时、
什么来源、推进了哪些键、前后短指纹），供管理台「Cookie 轮换流水」面板查询。

安全口径与 CookiePushHistory 一致：只记键名与 8 位短指纹，Cookie 值绝不
进入事件；进程内环形队列，重启即清空，不落盘。
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field

from aistudio_api.infrastructure.utils.logger import get_logger

logger = get_logger("rotation_history")

_SHORT_HASH_LEN = 8
_MAX_EVENT_CHANGES = 24


@dataclass
class RotationChange:
    """单个 Cookie 键的前后代次（短指纹）。"""

    name: str
    domain: str
    before: str | None = None
    after: str | None = None


@dataclass
class RotationEvent:
    seq: int
    at: float
    account_id: str
    source: str
    kind: str
    reason: str = ""
    cookie_count: int = 0
    fingerprint: str = ""
    previous_fingerprint: str | None = None
    changes: list[RotationChange] = field(default_factory=list)


def short_digest(value: object) -> str:
    raw = str(value or "")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:_SHORT_HASH_LEN]


class CookieRotationHistory:
    """有界线程安全的代次推进流水。"""

    def __init__(self, capacity: int = 300) -> None:
        self._events: deque[RotationEvent] = deque(maxlen=max(1, capacity))
        self._seq = 0
        self._lock = threading.Lock()
        self._last_fingerprint: dict[str, str] = {}

    def record(
        self,
        *,
        account_id: str,
        source: str,
        kind: str,
        changes: list[RotationChange] | None = None,
        reason: str = "",
        cookie_count: int = 0,
        fingerprint: str = "",
    ) -> RotationEvent | None:
        """记录一次代次推进；无变更键时返回 None 不记录。

        source: 推进路径（save / probe / rotate / harvest / push）
        kind:   事件语义（rotated=代次前进 / updated=常规更新）
        """
        trimmed = (changes or [])[:_MAX_EVENT_CHANGES]
        if not trimmed and fingerprint == self._last_fingerprint.get(account_id):
            return None
        with self._lock:
            self._seq += 1
            event = RotationEvent(
                seq=self._seq,
                at=time.time(),
                account_id=account_id,
                source=source,
                kind=kind if trimmed else "updated",
                reason=reason,
                cookie_count=cookie_count,
                fingerprint=fingerprint,
                previous_fingerprint=self._last_fingerprint.get(account_id),
                changes=trimmed,
            )
            self._events.append(event)
            if fingerprint:
                self._last_fingerprint[account_id] = fingerprint
            return event

    def note_fingerprint(self, account_id: str, fingerprint: str) -> None:
        """只更新基线指纹（比如账号刚保存但无键变化时），不产生事件。"""
        with self._lock:
            self._last_fingerprint[account_id] = fingerprint

    def recent(self, limit: int = 100) -> list[RotationEvent]:
        with self._lock:
            events = list(self._events)
        if limit > 0:
            events = events[-limit:]
        events.reverse()
        return events

    def clear(self) -> int:
        with self._lock:
            cleared = len(self._events)
            self._events.clear()
            self._last_fingerprint.clear()
            self._seq = 0
            return cleared


_rotation_history = CookieRotationHistory()


def get_rotation_history() -> CookieRotationHistory:
    return _rotation_history


def diff_cookie_values_as_changes(
    before: dict[str, str] | None,
    after: dict[str, str] | None,
) -> list[RotationChange]:
    """对比两份 name->value 快照，产出带短指纹的变化列表。"""
    before = before or {}
    after = after or {}
    changes: list[RotationChange] = []
    for name in sorted(set(before) | set(after)):
        old, new = before.get(name), after.get(name)
        if old == new:
            continue
        changes.append(
            RotationChange(
                name=name,
                domain="",
                before=short_digest(old) if old else None,
                after=short_digest(new) if new else None,
            )
        )
    return changes


def snapshot_cookie_values(cookies: list[dict[str, object]] | None) -> dict[str, str]:
    """把 Cookie 列表压成 name->value 映射（供 diff 用，不外发）。"""
    snap: dict[str, str] = {}
    for item in cookies or []:
        if isinstance(item, dict) and item.get("name"):
            snap[str(item["name"])] = str(item.get("value") or "")
    return snap


def event_to_dict(event: RotationEvent) -> dict[str, object]:
    data = asdict(event)
    data["changes"] = [asdict(c) for c in event.changes]
    return data


__all__ = [
    "CookieRotationHistory",
    "RotationChange",
    "RotationEvent",
    "diff_cookie_values_as_changes",
    "event_to_dict",
    "get_rotation_history",
    "short_digest",
    "snapshot_cookie_values",
]
