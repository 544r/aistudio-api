"""账号登录态巡检与 401 分类测试。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from aistudio_api.application.account_health import (
    check_all_accounts,
    probe_account_session,
)
from aistudio_api.application.account_rotator import AccountStats
from aistudio_api.domain.errors import (
    AuthError,
    SessionExpiredError,
    UsageLimitExceeded,
    classify_error,
)


def test_upstream_401_is_classified_as_session_expired():
    """401/UNAUTHENTICATED 必须视为 Cookie 失效，而不是普通鉴权冷却。"""
    exc = classify_error(
        401, '[[16, "Request had invalid authentication credentials"]]'
    )
    assert isinstance(exc, SessionExpiredError)

    grpc_exc = classify_error(500, '[[16, "UNAUTHENTICATED"]]')
    assert isinstance(grpc_exc, SessionExpiredError)


def test_403_and_429_keep_existing_classification():
    assert isinstance(classify_error(403, "[[7, 'PERMISSION_DENIED']]"), AuthError)
    assert isinstance(
        classify_error(429, "[[8, 'RESOURCE_EXHAUSTED']]"), UsageLimitExceeded
    )


def test_account_stats_session_expired_toggle():
    stats = AccountStats(account_id="acc")
    stats.set_session_expired(True)
    assert stats.session_expired is True
    assert stats.auth_cooldown > 0

    stats.set_session_expired(False)
    assert stats.session_expired is False
    assert stats.last_session_expired == 0.0
    assert stats.auth_cooldown == 0.0


class _FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _FakeAsyncClient:
    def __init__(
        self, status_code: int | None = None, exc: Exception | None = None
    ) -> None:
        self._status_code = status_code
        self._exc = exc

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    async def post(self, *args: object, **kwargs: object) -> _FakeResponse:
        if self._exc is not None:
            raise self._exc
        assert self._status_code is not None
        return _FakeResponse(self._status_code)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected"),
    [(200, True), (400, True), (403, True), (401, False), (500, None)],
)
async def test_probe_account_session_status_mapping(monkeypatch, status, expected):
    import aistudio_api.application.account_health as health

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda *args, **kwargs: _FakeAsyncClient(status)
    )
    result = await health.probe_account_session({"SAPISID": "x", "SID": "y"})
    assert result is expected


@pytest.mark.asyncio
async def test_probe_account_session_handles_network_error(monkeypatch):
    import aistudio_api.application.account_health as health

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *args, **kwargs: _FakeAsyncClient(exc=httpx.ConnectError("boom")),
    )
    assert await health.probe_account_session({"SAPISID": "x"}) is None


@pytest.mark.asyncio
async def test_probe_account_session_rejects_empty_cookies():
    assert await probe_account_session({}) is False


@pytest.mark.asyncio
async def test_check_all_accounts_updates_rotator(tmp_path, monkeypatch):
    """巡检只把 401 标记为失效，网络异常保持原状态。"""
    import aistudio_api.application.account_health as health

    def _write_auth(name: str):
        path = tmp_path / f"{name}.json"
        path.write_text(
            json.dumps({"cookies": [{"name": "SID", "value": name}]}),
            encoding="utf-8",
        )
        return path

    accounts = [
        SimpleNamespace(
            id="acc_valid",
            enabled=True,
            auth_user="0",
            auth_path=_write_auth("valid"),
        ),
        SimpleNamespace(
            id="acc_expired",
            enabled=True,
            auth_user="1",
            auth_path=_write_auth("expired"),
        ),
        SimpleNamespace(
            id="acc_unknown",
            enabled=True,
            auth_user="2",
            auth_path=_write_auth("unknown"),
        ),
        SimpleNamespace(
            id="acc_disabled",
            enabled=False,
            auth_user="3",
            auth_path=_write_auth("disabled"),
        ),
    ]
    account_service = SimpleNamespace(
        list_accounts=lambda: accounts,
        get_active_account=lambda: accounts[0],
        get_auth_path=lambda account_id: next(
            a.auth_path for a in accounts if a.id == account_id
        ),
    )

    calls: list[tuple[str, bool]] = []
    rotator = SimpleNamespace(
        set_session_expired=lambda account_id, expired: calls.append(
            (account_id, expired)
        )
    )

    async def fake_probe(cookies: dict[str, str], auth_user: str = "0"):
        value = next(iter(cookies.values()))
        return {"valid": True, "expired": False, "unknown": None}[value]

    monkeypatch.setattr(health, "probe_account_session", fake_probe)
    monkeypatch.setattr(health.runtime_state, "client", None)

    summary = await check_all_accounts(account_service, rotator)  # type: ignore[arg-type]

    assert summary == {
        "checked": 3,
        "valid": 1,
        "expired": ["acc_expired"],
        "unknown": ["acc_unknown"],
    }
    assert ("acc_valid", False) in calls
    assert ("acc_expired", True) in calls
    assert all(account_id != "acc_disabled" for account_id, _ in calls)
