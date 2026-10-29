"""Browser cookie push history for the web console.

The console needs to show *what changed* between pushes without exposing
session cookie values.  This module keeps only cookie names, domains, paths,
short value fingerprints and the per-push diff.  Raw values never leave the
push request path and the history is intentionally process-local.
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TypeAlias

CORE_COOKIE_NAMES = frozenset(
    {
        "SID",
        "HSID",
        "SSID",
        "SAPISID",
        "APISID",
        "__Secure-1PAPISID",
        "__Secure-3PAPISID",
        "__Secure-1PSID",
        "__Secure-3PSID",
    }
)
ROLLING_COOKIE_NAMES = frozenset({"__Secure-1PSIDTS", "__Secure-3PSIDTS"})
ADVISORY_SESSION_COOKIE_NAMES = ("__Secure-1PSID", "__Secure-3PSID")

_CATEGORY_ORDER = {"core": 0, "rolling": 1, "other": 2}
_SHORT_HASH_LEN = 8

# key -> (name, domain, path, full value sha256)
CookieSnapshot: TypeAlias = dict[str, tuple[str, str, str, str]]


@dataclass
class CookiePushChange:
    name: str
    domain: str
    path: str
    category: str
    kind: str
    before: str | None = None
    after: str | None = None


@dataclass
class CookiePushEvent:
    seq: int
    at: float
    account_id: str
    name: str
    email: str | None
    auth_user: str
    source: str | None
    received_count: int
    cookie_count: int
    fingerprint: str
    merged_fingerprint: str
    previous_fingerprint: str | None
    first: bool
    changed: bool
    merged_changed: bool
    changes: list[CookiePushChange]
    missing_advisory: list[str]
    merged_missing_advisory: list[str]
    session_valid: bool | None
    injected: bool
    created: bool
    updated_accounts: int


class CookiePushHistory:
    """Bounded, thread-safe, in-memory push history."""

    def __init__(self, capacity: int = 200) -> None:
        self._capacity = max(1, capacity)
        self._events: deque[CookiePushEvent] = deque(maxlen=self._capacity)
        self._last_raw: dict[str, tuple[str, CookieSnapshot]] = {}
        self._last_merged_fingerprint: dict[str, str] = {}
        self._seq = 0
        self._lock = threading.Lock()

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def total(self) -> int:
        with self._lock:
            return len(self._events)

    def record(
        self,
        *,
        account_id: str,
        name: str,
        email: str | None,
        auth_user: str,
        source: str | None,
        raw_cookies: Iterable[dict[str, object]],
        merged_cookies: Iterable[dict[str, object]],
        received_count: int,
        session_valid: bool | None,
        injected: bool,
        created: bool,
        updated_accounts: int,
    ) -> CookiePushEvent:
        raw_snapshot = _snapshot(raw_cookies)
        merged_snapshot = _snapshot(merged_cookies)
        raw_fingerprint = _fingerprint(raw_snapshot)
        merged_fingerprint = _fingerprint(merged_snapshot)
        at = time.time()

        with self._lock:
            previous = self._last_raw.get(account_id)
            previous_fingerprint = previous[0] if previous is not None else None
            first = previous is None
            changed = previous is not None and previous_fingerprint != raw_fingerprint
            changes = _diff(previous[1], raw_snapshot) if previous is not None else []
            previous_merged = self._last_merged_fingerprint.get(account_id)
            merged_changed = (
                previous_merged is not None and previous_merged != merged_fingerprint
            )

            self._seq += 1
            event = CookiePushEvent(
                seq=self._seq,
                at=at,
                account_id=account_id,
                name=name,
                email=email,
                auth_user=auth_user,
                source=source,
                received_count=received_count,
                cookie_count=len(raw_snapshot),
                fingerprint=raw_fingerprint,
                merged_fingerprint=merged_fingerprint,
                previous_fingerprint=previous_fingerprint,
                first=first,
                changed=changed,
                merged_changed=merged_changed,
                changes=changes,
                missing_advisory=_missing_advisory(raw_snapshot),
                merged_missing_advisory=_missing_advisory(merged_snapshot),
                session_valid=session_valid,
                injected=injected,
                created=created,
                updated_accounts=updated_accounts,
            )
            self._events.append(event)
            self._last_raw[account_id] = (raw_fingerprint, raw_snapshot)
            self._last_merged_fingerprint[account_id] = merged_fingerprint
            return event

    def recent(self, limit: int = 100) -> list[CookiePushEvent]:
        """Return newest-first events, capped at ``limit``."""
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
            self._last_raw.clear()
            self._last_merged_fingerprint.clear()
            return cleared


def _snapshot(cookies: Iterable[dict[str, object]]) -> CookieSnapshot:
    snapshot: CookieSnapshot = {}
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        name = str(cookie.get("name") or "")
        if not name:
            continue
        domain = str(cookie.get("domain") or "").lower()
        path = str(cookie.get("path") or "/") or "/"
        key = f"{name}|{domain}|{path}"
        digest = hashlib.sha256(
            str(cookie.get("value") or "").encode("utf-8")
        ).hexdigest()
        snapshot[key] = (name, domain, path, digest)
    return snapshot


def _fingerprint(snapshot: CookieSnapshot) -> str:
    canonical = "\n".join(f"{key}={snapshot[key][3]}" for key in sorted(snapshot))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def _category(name: str) -> str:
    if name in CORE_COOKIE_NAMES:
        return "core"
    if name in ROLLING_COOKIE_NAMES:
        return "rolling"
    return "other"


def _missing_advisory(snapshot: CookieSnapshot) -> list[str]:
    names = {entry[0] for entry in snapshot.values()}
    return [name for name in ADVISORY_SESSION_COOKIE_NAMES if name not in names]


def _diff(base: CookieSnapshot, current: CookieSnapshot) -> list[CookiePushChange]:
    changes: list[CookiePushChange] = []

    for key, (name, domain, path, digest) in current.items():
        previous = base.get(key)
        if previous is None:
            changes.append(
                CookiePushChange(
                    name=name,
                    domain=domain,
                    path=path,
                    category=_category(name),
                    kind="added",
                    before=None,
                    after=digest[:_SHORT_HASH_LEN],
                )
            )
        elif previous[3] != digest:
            changes.append(
                CookiePushChange(
                    name=name,
                    domain=domain,
                    path=path,
                    category=_category(name),
                    kind="changed",
                    before=previous[3][:_SHORT_HASH_LEN],
                    after=digest[:_SHORT_HASH_LEN],
                )
            )

    for key, (name, domain, path, digest) in base.items():
        if key not in current:
            changes.append(
                CookiePushChange(
                    name=name,
                    domain=domain,
                    path=path,
                    category=_category(name),
                    kind="removed",
                    before=digest[:_SHORT_HASH_LEN],
                    after=None,
                )
            )

    changes.sort(
        key=lambda change: (
            _CATEGORY_ORDER.get(change.category, 9),
            change.name,
            change.domain,
            change.path,
        )
    )
    return changes


__all__ = [
    "ADVISORY_SESSION_COOKIE_NAMES",
    "CORE_COOKIE_NAMES",
    "ROLLING_COOKIE_NAMES",
    "CookiePushChange",
    "CookiePushEvent",
    "CookiePushHistory",
]
