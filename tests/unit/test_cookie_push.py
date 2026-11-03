"""Tests for the browser cookie push endpoint and normalization helpers."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import urlencode

import httpx
import pytest

from aistudio_api.api.app import (
    _DUMP_SENSITIVE_HEADERS,
    _DUMP_SENSITIVE_QUERY_KEYS,
    _mask_dump_value,
    _masked_dump_url,
    app,
)
from aistudio_api.api.state import runtime_state
from aistudio_api.application.account_service import AccountService
from aistudio_api.application.cookie_push_history import CookiePushHistory
from aistudio_api.infrastructure.account.account_store import AccountMeta
from aistudio_api.infrastructure.account.cookie_parser import (
    build_storage_state_from_browser_cookies,
    missing_session_cookies,
)


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


def _cookie(name: str, value: str, **extra: object) -> dict[str, object]:
    item: dict[str, object] = {
        "name": name,
        "value": value,
        "domain": ".google.com",
        "path": "/",
        "expirationDate": 1893456000,
        "httpOnly": True,
        "secure": True,
        "sameSite": "no_restriction",
    }
    item.update(extra)
    return item


def _full_jar() -> list[dict[str, object]]:
    return [
        _cookie("SID", "sid-value"),
        _cookie("SAPISID", "sapisid-value"),
        _cookie("__Secure-1PAPISID", "papisid1-value"),
        _cookie("__Secure-1PSID", "psid1-value"),
        _cookie("__Secure-3PSID", "psid3-value"),
        _cookie("HSID", "hsid-value"),
    ]


def _account(
    account_id: str = "acc_1",
    *,
    email: str | None = "user@example.com",
    auth_user: str = "0",
    cookie_id: str | None = "cookie_1",
    name: str = "Test Account",
    tier: str = "Free",
) -> AccountMeta:
    return AccountMeta(
        id=account_id,
        name=name,
        email=email,
        created_at="2026-10-17T00:00:00Z",
        last_used=None,
        auth_user=auth_user,
        cookie_id=cookie_id,
        tier=tier,
    )


def _mock_service(accounts: list[AccountMeta]) -> MagicMock:
    svc = MagicMock(spec=AccountService)
    svc.list_accounts.return_value = accounts
    svc.get_active_account.return_value = None

    def fake_get(account_id: str) -> AccountMeta | None:
        return next((a for a in accounts if a.id == account_id), None)

    svc.get_account.side_effect = fake_get
    svc.get_account_auth_path.return_value = None
    return svc


@pytest.fixture(autouse=True)
def restore_runtime_state():
    original_service = runtime_state.account_service
    original_client = runtime_state.client
    original_rotator = runtime_state.rotator
    runtime_state.cookie_push_history.clear()
    try:
        yield
    finally:
        runtime_state.account_service = original_service
        runtime_state.client = original_client
        runtime_state.rotator = original_rotator
        runtime_state.cookie_push_history.clear()


@pytest.fixture(autouse=True)
def stub_probe(monkeypatch):
    async def fake_probe(
        _cookies, auth_user="0", *, timeout=8.0, persist_to=None, set_cookie_sink=None
    ):
        return True

    async def fake_tier(_cookies, auth_user="0"):
        return "Free"

    async def fake_email(_cookies, auth_user="0"):
        # 默认视为探测失败：走上报邮箱，保持既有用例的语义
        return None

    monkeypatch.setattr(
        "aistudio_api.application.account_health.probe_account_session",
        fake_probe,
    )
    monkeypatch.setattr(
        "aistudio_api.api.routes_accounts.probe_account_benefit_tier",
        fake_tier,
    )
    monkeypatch.setattr(
        "aistudio_api.api.routes_accounts.probe_account_email",
        fake_email,
    )


# ==================== 归一化 / 完整性检查 ====================


def test_build_storage_state_maps_gm_cookie_fields():
    state = build_storage_state_from_browser_cookies(
        [
            _cookie("SID", "sid-value"),
            _cookie("SAPISID", "sapisid-value", sameSite="lax", secure=False),
            _cookie("session-only", "v", expirationDate=-1),
        ]
    )
    cookies = state["cookies"]
    assert isinstance(cookies, list)
    assert len(cookies) == 3

    sid = cookies[0]
    assert sid["name"] == "SID"
    assert sid["domain"] == ".google.com"
    assert sid["expires"] == 1893456000.0
    assert sid["sameSite"] == "None"
    assert sid["secure"] is True
    assert sid["httpOnly"] is True

    sapisid = cookies[1]
    assert sapisid["sameSite"] == "Lax"
    assert sapisid["secure"] is False
    # 签名票默认不对 JS 隐藏（需要读出来算 SAPISIDHASH）
    assert sapisid["httpOnly"] is True  # 显式传入的 httpOnly 优先

    session_only = cookies[2]
    assert session_only["expires"] == -1.0


def test_build_storage_state_filters_non_google_and_tracking_cookies():
    state = build_storage_state_from_browser_cookies(
        [
            _cookie("SID", "v"),
            _cookie("_ga", "x"),
            _cookie("_gcl_au", "x"),
            _cookie("OSID", "x", domain="aistudio.google.com"),
            _cookie("foo", "x", domain=".example.com"),
            _cookie("bar", "x", domain=""),
        ]
    )
    cookies = state["cookies"]
    assert isinstance(cookies, list)
    assert [c["name"] for c in cookies] == ["SID"]


def test_build_storage_state_keeps_rolling_session_cookies():
    """SIDCC / *PSIDCC 属于 Google 全域会话链路，导入时不能再当噪声丢掉。"""
    state = build_storage_state_from_browser_cookies(
        [
            _cookie("SID", "v"),
            _cookie("SIDCC", "sidcc_val"),
            _cookie("__Secure-1PSIDCC", "cc1"),
            _cookie("__Secure-1PSIDTS", "ts1"),
        ]
    )
    names = {c["name"] for c in state["cookies"] if isinstance(c, dict)}
    assert names == {"SID", "SIDCC", "__Secure-1PSIDCC", "__Secure-1PSIDTS"}


def test_build_storage_state_keeps_same_name_on_different_domains():
    state = build_storage_state_from_browser_cookies(
        [
            _cookie("__Secure-1PSIDTS", "a", domain=".google.com"),
            _cookie("__Secure-1PSIDTS", "b", domain="aistudio.google.com"),
            _cookie("__Secure-1PSIDTS", "a", domain=".google.com"),  # 完全重复
        ]
    )
    cookies = state["cookies"]
    assert isinstance(cookies, list)
    assert len(cookies) == 2
    assert {c["domain"] for c in cookies} == {".google.com", "aistudio.google.com"}


def test_missing_session_cookies():
    assert missing_session_cookies(["SID", "SAPISID", "__Secure-1PSID"]) == []
    # __Secure-*PSID 缺失在 Chrome 分区 Cookie 场景下是正常的，不再拒绝
    assert missing_session_cookies(["SID", "SAPISID"]) == []
    assert missing_session_cookies(["SID", "SAPISID", "__Secure-1PSIDTS"]) == []
    # 反向也成立：指纹浏览器 / GM_cookie 常常读不到 SID，但 __Secure-*PSID
    # 是完全等价的长寿命会话凭据，不能据此拒绝一份本来可用的罐子
    assert missing_session_cookies(["SAPISID", "__Secure-1PSID"]) == []
    assert missing_session_cookies(["SAPISID", "__Secure-3PSID"]) == []
    assert missing_session_cookies(["SAPISID", "__Secure-1PSIDTS"]) == ["SID"]
    assert missing_session_cookies(["SID", "__Secure-1PSID"]) == ["SAPISID"]
    assert missing_session_cookies([]) == ["SID", "SAPISID"]


def test_dump_metadata_masks_credentials():
    for header in (
        "authorization",
        "X-Api-Key",
        "x-goog-api-key",
        "Cookie",
        "Set-Cookie",
    ):
        assert _mask_dump_value(header, "secret", _DUMP_SENSITIVE_HEADERS) == "***"
    assert (
        _mask_dump_value("content-type", "application/json", _DUMP_SENSITIVE_HEADERS)
        == "application/json"
    )
    for key in ("key", "password", "token", "api_key"):
        assert _mask_dump_value(key, "secret", _DUMP_SENSITIVE_QUERY_KEYS) == "***"
    assert _mask_dump_value("alt", "sse", _DUMP_SENSITIVE_QUERY_KEYS) == "sse"

    url = _masked_dump_url(
        "http://test/api/accounts/push-cookies?key=secret&alt=sse",
        {"key": "***", "alt": "sse"},
    )
    assert "secret" not in url
    assert url == "http://test/api/accounts/push-cookies?" + urlencode(
        {"key": "***", "alt": "sse"}
    )
    assert _masked_dump_url("http://test/health", {}) == "http://test/health"


# ==================== 推送接口 ====================


@pytest.mark.asyncio
async def test_push_cookies_updates_matching_account():
    target = _account()
    svc = _mock_service([target])
    runtime_state.account_service = svc
    runtime_state.rotator = MagicMock()

    async with _client() as client:
        resp = await client.post(
            "/api/accounts/push-cookies",
            json={
                "cookies": _full_jar(),
                "email": "User@Example.com",
                "auth_user": "0",
            },
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["account_id"] == "acc_1"
    assert data["created"] is False
    assert data["updated_accounts"] == 1
    assert data["session_valid"] is True
    assert data["injected"] is False

    svc.save_account_from_cookies.assert_called_once()
    kwargs = svc.save_account_from_cookies.call_args.kwargs
    assert kwargs["account_id"] == "acc_1"
    assert kwargs["auth_user"] == "0"
    assert kwargs["cookie_id"] == "cookie_1"
    storage_state = kwargs["storage_state"]
    names = {c["name"] for c in storage_state["cookies"]}
    assert {"SID", "SAPISID", "__Secure-1PSID"} <= names

    runtime_state.rotator.set_session_expired.assert_called_with("acc_1", False)


@pytest.mark.asyncio
async def test_push_cookies_rejects_incomplete_jar():
    svc = _mock_service([_account()])
    runtime_state.account_service = svc

    async with _client() as client:
        resp = await client.post(
            "/api/accounts/push-cookies",
            json={"cookies": [_cookie("__Secure-1PSID", "v")]},
        )

    assert resp.status_code == 400
    detail = resp.json()["error"]["message"]
    assert "SID" in detail
    assert "SAPISID" in detail
    svc.save_account_from_cookies.assert_not_called()


@pytest.mark.asyncio
async def test_push_cookies_rejects_empty_google_set():
    runtime_state.account_service = _mock_service([])
    async with _client() as client:
        resp = await client.post(
            "/api/accounts/push-cookies",
            json={"cookies": [_cookie("foo", "v", domain=".example.com")]},
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_push_cookies_creates_unknown_account(monkeypatch):
    new_account = _account(
        "acc_new",
        email="new@example.com",
        cookie_id="cookie_new",
        name="new@example.com",
    )
    svc = _mock_service([])
    svc.save_account_from_cookies.return_value = new_account
    svc.get_account.side_effect = lambda account_id: (
        new_account if account_id == "acc_new" else None
    )
    runtime_state.account_service = svc
    runtime_state.rotator = MagicMock()

    async with _client() as client:
        resp = await client.post(
            "/api/accounts/push-cookies",
            json={"cookies": _full_jar(), "email": "new@example.com"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["created"] is True
    assert data["account_id"] == "acc_new"
    kwargs = svc.save_account_from_cookies.call_args.kwargs
    assert kwargs.get("account_id") is None
    assert kwargs["email"] == "new@example.com"
    assert kwargs["cookie_id"].startswith("cookie_")


@pytest.mark.asyncio
async def test_push_cookies_trusts_cookie_identity_over_reported_email(monkeypatch):
    """上报邮箱可能是从同浏览器另一个账号读到的，应以 Cookie 罐归属为准。"""
    target = _account(email="real@example.com")
    svc = _mock_service([target])
    runtime_state.account_service = svc
    runtime_state.rotator = MagicMock()

    async def fake_email(_cookies, auth_user="0"):
        return "real@example.com"

    monkeypatch.setattr(
        "aistudio_api.api.routes_accounts.probe_account_email", fake_email
    )

    async with _client() as client:
        resp = await client.post(
            "/api/accounts/push-cookies",
            json={"cookies": _full_jar(), "email": "typo@example.com"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["account_id"] == "acc_1"
    assert data["created"] is False
    kwargs = svc.save_account_from_cookies.call_args.kwargs
    assert kwargs["account_id"] == "acc_1"


@pytest.mark.asyncio
async def test_push_cookies_falls_back_when_binding_is_gone():
    """绑定的账号被删除后，推送要回退匹配，而不是直接 404。"""
    target = _account()
    svc = _mock_service([target])
    runtime_state.account_service = svc
    runtime_state.rotator = MagicMock()

    async with _client() as client:
        resp = await client.post(
            "/api/accounts/push-cookies",
            json={
                "cookies": _full_jar(),
                "account_id": "acc_deleted",
                "email": "user@example.com",
            },
        )

    assert resp.status_code == 200
    assert resp.json()["account_id"] == "acc_1"


@pytest.mark.asyncio
async def test_push_cookies_ignores_binding_for_other_account(monkeypatch):
    """绑定账号与本次 Cookie 属于不同账号时，只能匹配/新建，不能覆盖绑定账号。"""
    bound = _account("acc_bound", email="bound@example.com", cookie_id="cookie_a")
    other = _account("acc_other", email="other@example.com", cookie_id="cookie_b")
    svc = _mock_service([bound, other])
    runtime_state.account_service = svc
    runtime_state.rotator = MagicMock()

    async def fake_email(_cookies, auth_user="0"):
        return "other@example.com"

    monkeypatch.setattr(
        "aistudio_api.api.routes_accounts.probe_account_email", fake_email
    )

    async with _client() as client:
        resp = await client.post(
            "/api/accounts/push-cookies",
            json={
                "cookies": _full_jar(),
                "account_id": "acc_bound",
                "email": "other@example.com",
            },
        )

    assert resp.status_code == 200
    assert resp.json()["account_id"] == "acc_other"
    updated_ids = {
        call.kwargs["account_id"]
        for call in svc.save_account_from_cookies.call_args_list
    }
    assert updated_ids == {"acc_other"}


@pytest.mark.asyncio
async def test_push_cookies_refreshes_whole_cookie_group():
    target = _account("acc_1", cookie_id="cookie_shared")
    sibling = _account("acc_2", auth_user="1", cookie_id="cookie_shared")
    other = _account("acc_3", email="other@example.com", cookie_id="cookie_other")
    svc = _mock_service([target, sibling, other])
    runtime_state.account_service = svc
    runtime_state.rotator = MagicMock()

    async with _client() as client:
        resp = await client.post(
            "/api/accounts/push-cookies",
            json={"cookies": _full_jar(), "email": "user@example.com"},
        )

    assert resp.status_code == 200
    assert resp.json()["updated_accounts"] == 2
    updated_ids = {
        call.kwargs["account_id"]
        for call in svc.save_account_from_cookies.call_args_list
    }
    assert updated_ids == {"acc_1", "acc_2"}
    expired_calls = [
        call.args for call in runtime_state.rotator.set_session_expired.call_args_list
    ]
    assert ("acc_1", False) in expired_calls
    assert ("acc_2", False) in expired_calls


@pytest.mark.asyncio
async def test_push_cookies_discovers_all_sub_accounts(monkeypatch):
    target = _account("acc_1", cookie_id="cookie_shared")
    svc = _mock_service([target])
    runtime_state.account_service = svc
    runtime_state.rotator = MagicMock()

    async def fake_discover(_cookies):
        return [
            {"auth_user": "0", "email": "user@example.com", "tier": "Free"},
            {"auth_user": "1", "email": "second@example.com", "tier": "Pro"},
        ]

    monkeypatch.setattr(
        "aistudio_api.api.routes_accounts.probe_google_accounts_infinite",
        fake_discover,
    )

    async with _client() as client:
        resp = await client.post(
            "/api/accounts/push-cookies",
            json={
                "cookies": _full_jar(),
                "email": "user@example.com",
                "discover_all": True,
            },
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["updated_accounts"] == 1
    assert body["discovered_accounts"] == 1

    created = [
        call.kwargs
        for call in svc.save_account_from_cookies.call_args_list
        if call.kwargs.get("account_id") is None
    ]
    assert len(created) == 1
    assert created[0]["auth_user"] == "1"
    assert created[0]["email"] == "second@example.com"
    assert created[0]["cookie_id"] == "cookie_shared"
    assert created[0]["tier"] == "Pro"
    runtime_state.rotator.add_account.assert_called_once()


@pytest.mark.asyncio
async def test_push_cookies_discovery_updates_existing_and_skips_blocked(monkeypatch):
    from aistudio_api.config import settings

    monkeypatch.setattr(settings, "blocked_emails", frozenset({"main@example.com"}))
    target = _account("acc_1", cookie_id="cookie_shared")
    sibling = _account(
        "acc_2",
        email="second@example.com",
        auth_user="1",
        cookie_id="cookie_other",
    )
    svc = _mock_service([target, sibling])
    runtime_state.account_service = svc
    runtime_state.rotator = MagicMock()

    async def fake_discover(_cookies):
        return [
            {"auth_user": "0", "email": "user@example.com", "tier": "Free"},
            {"auth_user": "1", "email": "second@example.com", "tier": "Free"},
            {"auth_user": "2", "email": "main@example.com", "tier": "Free"},
        ]

    monkeypatch.setattr(
        "aistudio_api.api.routes_accounts.probe_google_accounts_infinite",
        fake_discover,
    )

    async with _client() as client:
        resp = await client.post(
            "/api/accounts/push-cookies",
            json={
                "cookies": _full_jar(),
                "email": "user@example.com",
                "discover_all": True,
            },
        )

    assert resp.status_code == 200
    assert resp.json()["discovered_accounts"] == 1
    saved_ids = {
        call.kwargs.get("account_id")
        for call in svc.save_account_from_cookies.call_args_list
    }
    assert saved_ids == {"acc_1", "acc_2"}
    sibling_call = next(
        call.kwargs
        for call in svc.save_account_from_cookies.call_args_list
        if call.kwargs.get("account_id") == "acc_2"
    )
    # 已有账号沿用原来的 cookie 分组，避免把不同浏览器的罐并成一组
    assert sibling_call["cookie_id"] == "cookie_other"
    assert all(
        call.kwargs.get("email") != "main@example.com"
        for call in svc.save_account_from_cookies.call_args_list
    )


@pytest.mark.asyncio
async def test_push_cookies_discovery_failure_keeps_push_success(monkeypatch):
    target = _account()
    svc = _mock_service([target])
    runtime_state.account_service = svc
    runtime_state.rotator = MagicMock()

    async def boom(_cookies):
        raise RuntimeError("probe down")

    monkeypatch.setattr(
        "aistudio_api.api.routes_accounts.probe_google_accounts_infinite",
        boom,
    )

    async with _client() as client:
        resp = await client.post(
            "/api/accounts/push-cookies",
            json={
                "cookies": _full_jar(),
                "email": "user@example.com",
                "discover_all": True,
            },
        )

    assert resp.status_code == 200
    assert resp.json()["discovered_accounts"] == 0
    svc.save_account_from_cookies.assert_called_once()


@pytest.mark.asyncio
async def test_push_cookies_injects_into_active_browser():
    target = _account()
    svc = _mock_service([target])
    svc.get_active_account.return_value = target
    svc.get_account_auth_path.return_value = "data/accounts/acc_1/auth.json"
    runtime_state.account_service = svc
    runtime_state.rotator = MagicMock()

    session = MagicMock()
    session._auth_file = "data/accounts/acc_1/auth.json"
    session.apply_pushed_cookies = AsyncMock(return_value=True)
    runtime_state.client = MagicMock(_session=session)

    async with _client() as client:
        resp = await client.post(
            "/api/accounts/push-cookies",
            json={"cookies": _full_jar(), "account_id": "acc_1"},
        )

    assert resp.status_code == 200
    assert resp.json()["injected"] is True
    # 推送罐探活通过：接管滚动代次，注入时强制覆盖罐里的服务票
    session.apply_pushed_cookies.assert_awaited_once_with(
        "data/accounts/acc_1/auth.json", include_rolling=True
    )


@pytest.mark.asyncio
async def test_push_cookies_rescue_forces_service_ticket_injection():
    """推送前账号已判失效 → 推送罐探活通过后强制覆盖罐里的旧票。

    服务器实测（2026-11-03）：失效账号推送后，热注入按“罐优先”保留死会话的
    旧服务票，60 秒落盘又写回 auth.json，健康检查随即再次判失效；只有删号
    重建才恢复。推送接管必须强制覆盖。
    """
    target = _account()
    svc = _mock_service([target])
    svc.get_active_account.return_value = target
    svc.get_account_auth_path.return_value = "data/accounts/acc_1/auth.json"
    runtime_state.account_service = svc
    rotator = MagicMock()
    rotator.get_account_stats.return_value = SimpleNamespace(session_expired=True)
    runtime_state.rotator = rotator

    session = MagicMock()
    session._auth_file = "data/accounts/acc_1/auth.json"
    session.apply_pushed_cookies = AsyncMock(return_value=True)
    runtime_state.client = MagicMock(_session=session)

    async with _client() as client:
        resp = await client.post(
            "/api/accounts/push-cookies",
            json={"cookies": _full_jar(), "account_id": "acc_1"},
        )

    assert resp.status_code == 200
    session.apply_pushed_cookies.assert_awaited_once_with(
        "data/accounts/acc_1/auth.json", include_rolling=True
    )


@pytest.mark.asyncio
async def test_push_valid_jar_forces_injection_without_expired_flag():
    """账号还没被巡检判失效，但推送罐探活通过：也要接管罐里的旧票。

    旧逻辑只看调度器的 expired 标记；账号刚坏、巡检还没跑时标记是 False，
    推送的新票进不了浏览器，随后定时回写又用旧票覆盖 auth.json，表现就是
    “推送成功但没生效”。判据必须是推送罐自己的探活结果。
    """
    target = _account()
    svc = _mock_service([target])
    svc.get_active_account.return_value = target
    svc.get_account_auth_path.return_value = "data/accounts/acc_1/auth.json"
    runtime_state.account_service = svc
    rotator = MagicMock()
    rotator.get_account_stats.return_value = SimpleNamespace(session_expired=False)
    runtime_state.rotator = rotator

    session = MagicMock()
    session._auth_file = "data/accounts/acc_1/auth.json"
    session.apply_pushed_cookies = AsyncMock(return_value=True)
    runtime_state.client = MagicMock(_session=session)

    async with _client() as client:
        resp = await client.post(
            "/api/accounts/push-cookies",
            json={"cookies": _full_jar(), "account_id": "acc_1"},
        )

    assert resp.status_code == 200
    assert resp.json()["session_valid"] is True
    session.apply_pushed_cookies.assert_awaited_once_with(
        "data/accounts/acc_1/auth.json", include_rolling=True
    )


@pytest.mark.asyncio
async def test_push_cookies_skips_injection_without_browser():
    target = _account()
    svc = _mock_service([target])
    svc.get_active_account.return_value = target
    runtime_state.account_service = svc
    runtime_state.rotator = MagicMock()
    runtime_state.client = None

    async with _client() as client:
        resp = await client.post(
            "/api/accounts/push-cookies",
            json={"cookies": _full_jar(), "account_id": "acc_1"},
        )

    assert resp.status_code == 200
    assert resp.json()["injected"] is False


@pytest.mark.asyncio
async def test_push_cookies_marks_session_invalid_when_probe_fails(monkeypatch):
    async def fake_probe(
        _cookies, auth_user="0", *, timeout=8.0, persist_to=None, set_cookie_sink=None
    ):
        return False

    monkeypatch.setattr(
        "aistudio_api.application.account_health.probe_account_session",
        fake_probe,
    )
    target = _account()
    svc = _mock_service([target])
    runtime_state.account_service = svc
    runtime_state.rotator = MagicMock()

    async with _client() as client:
        resp = await client.post(
            "/api/accounts/push-cookies",
            json={"cookies": _full_jar(), "account_id": "acc_1"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["session_valid"] is False
    # 明确失败才标记失效；不再先无条件清除再根据结果回填
    runtime_state.rotator.set_session_expired.assert_called_once_with("acc_1", True)


@pytest.mark.asyncio
async def test_push_cookies_keeps_expired_state_when_probe_unknown(monkeypatch):
    async def fake_probe(
        _cookies, auth_user="0", *, timeout=8.0, persist_to=None, set_cookie_sink=None
    ):
        return None

    monkeypatch.setattr(
        "aistudio_api.application.account_health.probe_account_session",
        fake_probe,
    )
    target = _account()
    svc = _mock_service([target])
    runtime_state.account_service = svc
    rotator = MagicMock()
    rotator.get_account_stats.return_value = SimpleNamespace(session_expired=True)
    runtime_state.rotator = rotator

    async with _client() as client:
        resp = await client.post(
            "/api/accounts/push-cookies",
            json={"cookies": _full_jar(), "account_id": "acc_1"},
        )

    assert resp.status_code == 200
    assert resp.json()["session_valid"] is None
    # 网络未知：既不能误判有效，也不能把已失效账号误恢复成可调度
    rotator.set_session_expired.assert_not_called()


# ==================== 禁用名单（主号保护） ====================


def test_disable_blocked_accounts_disables_matching(monkeypatch):
    from aistudio_api.application.account_service import AccountService as Service
    from aistudio_api.config import settings

    monkeypatch.setattr(settings, "blocked_emails", frozenset({"main@example.com"}))
    main = _account("acc_main", email="main@example.com", name="Main")
    alt = _account("acc_alt", email="alt@example.com", name="Alt")
    store = MagicMock()
    store.list_accounts.return_value = [main, alt]
    store.toggle_account_enabled.return_value = main

    disabled = Service(store).disable_blocked_accounts()

    assert [a.id for a in disabled] == ["acc_main"]
    store.toggle_account_enabled.assert_called_once_with("acc_main", False)


@pytest.mark.asyncio
async def test_push_cookies_rejects_blocked_email(monkeypatch):
    from aistudio_api.config import settings

    monkeypatch.setattr(settings, "blocked_emails", frozenset({"user@example.com"}))
    svc = _mock_service([_account()])
    runtime_state.account_service = svc

    async with _client() as client:
        resp = await client.post(
            "/api/accounts/push-cookies",
            json={"cookies": _full_jar(), "email": "User@Example.com"},
        )

    assert resp.status_code == 403
    assert "AISTUDIO_BLOCKED_EMAILS" in resp.json()["error"]["message"]
    svc.save_account_from_cookies.assert_not_called()


@pytest.mark.asyncio
async def test_push_cookies_rejects_blocked_account_by_id(monkeypatch):
    from aistudio_api.config import settings

    monkeypatch.setattr(settings, "blocked_emails", frozenset({"user@example.com"}))
    target = _account()  # 账号本身就在禁用名单里
    svc = _mock_service([target])
    runtime_state.account_service = svc

    async with _client() as client:
        resp = await client.post(
            "/api/accounts/push-cookies",
            json={"cookies": _full_jar(), "account_id": "acc_1"},
        )

    assert resp.status_code == 403
    svc.save_account_from_cookies.assert_not_called()


@pytest.mark.asyncio
async def test_push_cookies_accepts_jar_without_secure_psid():
    """Chrome 分区 Cookie 场景下扩展读不到 __Secure-*PSID，不应拒绝推送。"""
    target = _account()
    svc = _mock_service([target])
    runtime_state.account_service = svc
    runtime_state.rotator = MagicMock()

    jar = [
        _cookie("SID", "sid-value"),
        _cookie("SAPISID", "sapisid-value"),
        _cookie("__Secure-1PSIDTS", "ts-value"),
    ]
    async with _client() as client:
        resp = await client.post(
            "/api/accounts/push-cookies",
            json={"cookies": jar, "email": "user@example.com"},
        )

    assert resp.status_code == 200
    assert resp.json()["session_valid"] is True


@pytest.mark.asyncio
async def test_push_cookies_merges_missing_cookies_from_existing_auth(tmp_path):
    """推送罐缺项时与现有 auth.json 合并：新值覆盖、旧值保留。"""
    target = _account()
    svc = _mock_service([target])
    auth_file = tmp_path / "auth.json"
    auth_file.write_text(
        json.dumps(
            {
                "cookies": [
                    {
                        "name": "SID",
                        "value": "old-sid",
                        "domain": ".google.com",
                        "path": "/",
                        "expires": -1,
                        "secure": True,
                        "httpOnly": True,
                    },
                    {
                        "name": "__Secure-1PSID",
                        "value": "old-psid",
                        "domain": ".google.com",
                        "path": "/",
                        "expires": -1,
                        "secure": True,
                        "httpOnly": True,
                    },
                ],
                "origins": [],
            }
        ),
        encoding="utf-8",
    )
    svc.get_account_auth_path.return_value = auth_file
    runtime_state.account_service = svc
    runtime_state.rotator = MagicMock()

    jar = [_cookie("SID", "new-sid"), _cookie("SAPISID", "sapisid-value")]
    async with _client() as client:
        resp = await client.post(
            "/api/accounts/push-cookies",
            json={"cookies": jar, "account_id": "acc_1"},
        )

    assert resp.status_code == 200
    saved = svc.save_account_from_cookies.call_args.kwargs["storage_state"]
    by_name = {c["name"]: c["value"] for c in saved["cookies"]}
    assert by_name["SID"] == "new-sid"
    assert by_name["SAPISID"] == "sapisid-value"
    assert by_name["__Secure-1PSID"] == "old-psid"


# ==================== 推送历史 ====================


def _record_history(
    history: CookiePushHistory,
    jar: list[dict[str, object]],
    *,
    merged: list[dict[str, object]] | None = None,
    account_id: str = "acc_1",
):
    return history.record(
        account_id=account_id,
        name="Test Account",
        email="u***@example.com",
        auth_user="0",
        source="userscript",
        raw_cookies=jar,
        merged_cookies=merged if merged is not None else jar,
        received_count=len(jar),
        session_valid=True,
        injected=False,
        created=False,
        updated_accounts=1,
    )


def test_cookie_push_history_first_and_identical_push():
    history = CookiePushHistory()
    jar = [*_full_jar(), _cookie("__Secure-1PSIDTS", "ts-one")]

    first = _record_history(history, jar)
    assert first.first is True
    assert first.changed is False
    assert first.changes == []
    assert first.cookie_count == len(jar)
    assert first.fingerprint

    same = _record_history(history, jar)
    assert same.first is False
    assert same.changed is False
    assert same.changes == []
    assert same.previous_fingerprint == first.fingerprint


def test_cookie_push_history_marks_rolling_and_core_changes():
    history = CookiePushHistory()
    _record_history(history, [*_full_jar(), _cookie("__Secure-1PSIDTS", "ts-one")])

    rolling = _record_history(
        history, [*_full_jar(), _cookie("__Secure-1PSIDTS", "ts-two")]
    )
    assert rolling.changed is True
    assert [change.name for change in rolling.changes] == ["__Secure-1PSIDTS"]
    assert rolling.changes[0].category == "rolling"
    assert rolling.changes[0].kind == "changed"
    assert rolling.changes[0].before != rolling.changes[0].after

    core_jar = [
        _cookie("SID", "new-sid"),
        *[cookie for cookie in _full_jar() if cookie["name"] != "SID"],
        _cookie("__Secure-1PSIDTS", "ts-two"),
    ]
    core = _record_history(history, core_jar)
    assert core.changed is True
    assert [change.name for change in core.changes] == ["SID"]
    assert core.changes[0].category == "core"
    assert core.changes[0].kind == "changed"


def test_cookie_push_history_reports_added_removed_and_missing_advisory():
    history = CookiePushHistory()
    _record_history(
        history,
        [
            _cookie("SID", "v"),
            _cookie("SAPISID", "s"),
            _cookie("__Secure-1PSID", "p"),
        ],
    )
    event = _record_history(
        history,
        [
            _cookie("SID", "v"),
            _cookie("SAPISID", "s"),
            _cookie("__Secure-3PSID", "p3"),
        ],
    )

    kinds = {(change.name, change.kind) for change in event.changes}
    assert ("__Secure-1PSID", "removed") in kinds
    assert ("__Secure-3PSID", "added") in kinds
    assert event.missing_advisory == ["__Secure-1PSID"]
    assert event.merged_missing_advisory == ["__Secure-1PSID"]


def test_cookie_push_history_tracks_merged_jar_changes():
    history = CookiePushHistory()
    raw = [_cookie("SID", "v"), _cookie("SAPISID", "s")]
    _record_history(
        history,
        raw,
        merged=[*raw, _cookie("__Secure-1PSID", "old")],
    )
    event = _record_history(
        history,
        raw,
        merged=[*raw, _cookie("__Secure-1PSID", "new")],
    )

    assert event.changed is False
    assert event.merged_changed is True
    assert event.changes == []


def test_cookie_push_history_is_bounded_and_keeps_only_fingerprints():
    history = CookiePushHistory(capacity=2)
    for index in range(3):
        _record_history(history, [_cookie("SID", f"secret-{index}")])

    assert history.total == 2
    entries = history.recent()
    assert len(entries) == 2
    assert "secret-" not in repr(entries)
    assert entries[0].changes[0].name == "SID"
    assert entries[0].changes[0].before is not None
    assert entries[0].changes[0].after is not None
    assert history.clear() == 2
    assert history.total == 0


@pytest.mark.asyncio
async def test_push_cookies_records_history_without_raw_values():
    svc = _mock_service([_account()])
    runtime_state.account_service = svc
    runtime_state.rotator = MagicMock()

    first_jar = [*_full_jar(), _cookie("__Secure-1PSIDTS", "ts-one")]
    second_jar = [*_full_jar(), _cookie("__Secure-1PSIDTS", "ts-two")]
    async with _client() as client:
        first = await client.post(
            "/api/accounts/push-cookies",
            json={
                "cookies": first_jar,
                "email": "user@example.com",
                "source": "userscript",
            },
        )
        second = await client.post(
            "/api/accounts/push-cookies",
            json={
                "cookies": second_jar,
                "email": "user@example.com",
                "source": "userscript",
            },
        )
        history = await client.get("/api/accounts/push-history")

    assert first.status_code == 200
    assert second.status_code == 200
    assert history.status_code == 200
    payload = history.json()
    entries = payload["entries"]
    assert payload["total"] == 2
    assert len(entries) == 2

    latest, previous = entries
    assert latest["first"] is False
    assert latest["changed"] is True
    assert latest["previous_fingerprint"] == previous["fingerprint"]
    assert latest["cookie_count"] == len(second_jar)
    assert latest["received_count"] == len(second_jar)

    changes = {change["name"]: change for change in latest["changes"]}
    assert set(changes) == {"__Secure-1PSIDTS"}
    assert changes["__Secure-1PSIDTS"]["category"] == "rolling"
    assert changes["__Secure-1PSIDTS"]["kind"] == "changed"
    assert changes["__Secure-1PSIDTS"]["before"] != changes["__Secure-1PSIDTS"]["after"]

    assert "ts-one" not in history.text
    assert "ts-two" not in history.text
    assert "sid-value" not in history.text

    async with _client() as client:
        cleared = await client.delete("/api/accounts/push-history")
        after = await client.get("/api/accounts/push-history")

    assert cleared.status_code == 200
    assert cleared.json()["cleared"] == 2
    assert after.json()["total"] == 0


@pytest.mark.asyncio
async def test_push_cookies_auto_detects_pro_tier(monkeypatch):
    svc = _mock_service([_account()])
    runtime_state.account_service = svc
    runtime_state.rotator = MagicMock()

    async def fake_tier(_cookies, auth_user="0"):
        return "Pro"

    monkeypatch.setattr(
        "aistudio_api.api.routes_accounts.probe_account_benefit_tier",
        fake_tier,
    )

    async with _client() as client:
        resp = await client.post(
            "/api/accounts/push-cookies",
            json={"cookies": _full_jar(), "email": "user@example.com"},
        )

    assert resp.status_code == 200
    svc.update_account_tier.assert_called_once_with("acc_1", "Pro")


@pytest.mark.asyncio
async def test_push_cookies_skips_tier_probe_for_paid_account(monkeypatch):
    svc = _mock_service([_account(tier="Pro")])
    runtime_state.account_service = svc
    runtime_state.rotator = MagicMock()
    calls = 0

    async def fake_tier(_cookies, auth_user="0"):
        nonlocal calls
        calls += 1
        return "Pro"

    monkeypatch.setattr(
        "aistudio_api.api.routes_accounts.probe_account_benefit_tier",
        fake_tier,
    )

    async with _client() as client:
        resp = await client.post(
            "/api/accounts/push-cookies",
            json={"cookies": _full_jar(), "email": "user@example.com"},
        )

    assert resp.status_code == 200
    assert calls == 0
    svc.update_account_tier.assert_not_called()


def _rolling_auth_file(tmp_path, *, server_rotated: bool, ts_value: str) -> Path:
    state: dict[str, object] = {
        "cookies": [
            {
                "name": "SID",
                "value": "sid",
                "domain": ".google.com",
                "path": "/",
                "expires": -1,
                "secure": True,
                "httpOnly": True,
            },
            {
                "name": "__Secure-1PSIDTS",
                "value": ts_value,
                "domain": ".google.com",
                "path": "/",
                "expires": -1,
                "secure": True,
                "httpOnly": True,
            },
        ],
        "origins": [],
    }
    if server_rotated:
        state["server_rotated"] = True
    auth_file = tmp_path / "auth.json"
    auth_file.write_text(json.dumps(state), encoding="utf-8")
    return auth_file


def _auth_file_matching_full_jar(tmp_path, *, ts_value: str) -> Path:
    """长寿命凭据与 _full_jar 完全一致，只有滚动代次不同。"""
    cookies = [
        dict(item)
        for item in _full_jar()
        if item["name"]
        in {
            "SID",
            "SAPISID",
            "__Secure-1PSID",
            "__Secure-3PSID",
        }
    ]
    cookies.append(
        {
            "name": "__Secure-1PSIDTS",
            "value": ts_value,
            "domain": ".google.com",
            "path": "/",
            "expires": -1,
            "secure": True,
            "httpOnly": True,
        }
    )
    auth_file = tmp_path / "auth.json"
    auth_file.write_text(
        json.dumps({"cookies": cookies, "origins": []}), encoding="utf-8"
    )
    return auth_file


@pytest.mark.asyncio
async def test_push_overwrites_stale_generation_when_push_probe_passes(tmp_path):
    """推送罐探活通过：即便 auth.json 标着 server_rotated，也接受推送的新代次。

    服务端旧代次可能已经死了（或只是巡检还没发现）；只要推送罐实测可用，
    就应该让推送成为代次持有者，而不是守着旧值等下一次 401。
    """
    target = _account()
    svc = _mock_service([target])
    svc.get_account_auth_path.return_value = _rolling_auth_file(
        tmp_path, server_rotated=False, ts_value="old-gen"
    )
    runtime_state.account_service = svc
    runtime_state.rotator = MagicMock()

    jar = [
        _cookie("SID", "sid"),
        _cookie("SAPISID", "sapisid-value"),
        _cookie("__Secure-1PSIDTS", "fresh-gen"),
    ]
    async with _client() as client:
        resp = await client.post(
            "/api/accounts/push-cookies",
            json={"cookies": jar, "account_id": "acc_1"},
        )

    assert resp.status_code == 200
    saved = svc.save_account_from_cookies.call_args.kwargs["storage_state"]
    by_name = {c["name"]: c["value"] for c in saved["cookies"]}
    assert by_name["__Secure-1PSIDTS"] == "fresh-gen"


@pytest.mark.asyncio
async def test_push_verified_generation_overrides_server_rotated_auth(
    tmp_path, monkeypatch
):
    """推送罐探活通过时，server_rotated 不再是拒绝采纳的理由。"""

    async def fake_probe(
        _cookies, auth_user="0", *, timeout=8.0, persist_to=None, set_cookie_sink=None
    ):
        return True

    monkeypatch.setattr(
        "aistudio_api.application.account_health.probe_account_session",
        fake_probe,
    )
    target = _account()
    svc = _mock_service([target])
    svc.get_account_auth_path.return_value = _rolling_auth_file(
        tmp_path, server_rotated=True, ts_value="server-gen-9"
    )
    runtime_state.account_service = svc
    runtime_state.rotator = MagicMock()

    jar = [
        _cookie("SID", "sid"),
        _cookie("SAPISID", "sapisid-value"),
        _cookie("__Secure-1PSIDTS", "fresh-gen"),
    ]
    async with _client() as client:
        resp = await client.post(
            "/api/accounts/push-cookies",
            json={"cookies": jar, "account_id": "acc_1"},
        )

    assert resp.status_code == 200
    saved = svc.save_account_from_cookies.call_args.kwargs["storage_state"]
    by_name = {c["name"]: c["value"] for c in saved["cookies"]}
    assert by_name["__Secure-1PSIDTS"] == "fresh-gen"
    assert "server_rotated" not in saved


@pytest.mark.asyncio
async def test_push_preserves_generation_when_push_probe_fails(tmp_path, monkeypatch):
    """推送罐探活明确失败：保留服务端现有代次，不把可用的旧值回滚。"""

    async def fake_probe(
        _cookies, auth_user="0", *, timeout=8.0, persist_to=None, set_cookie_sink=None
    ):
        return False

    monkeypatch.setattr(
        "aistudio_api.application.account_health.probe_account_session",
        fake_probe,
    )
    target = _account()
    svc = _mock_service([target])
    svc.get_account_auth_path.return_value = _rolling_auth_file(
        tmp_path, server_rotated=True, ts_value="server-gen-9"
    )
    runtime_state.account_service = svc
    runtime_state.rotator = MagicMock()

    jar = [
        _cookie("SID", "sid"),
        _cookie("SAPISID", "sapisid-value"),
        _cookie("__Secure-1PSIDTS", "browser-gen-7"),
    ]
    async with _client() as client:
        resp = await client.post(
            "/api/accounts/push-cookies",
            json={"cookies": jar, "account_id": "acc_1"},
        )

    assert resp.status_code == 200
    saved = svc.save_account_from_cookies.call_args.kwargs["storage_state"]
    by_name = {c["name"]: c["value"] for c in saved["cookies"]}
    assert by_name["__Secure-1PSIDTS"] == "server-gen-9"


@pytest.mark.asyncio
async def test_push_keeps_server_generation_when_browser_session_alive(tmp_path):
    """推送罐可用、服务端浏览器罐也实测有效：滚动代次保持罐优先。

    两边都活着时让推送整体接管会变成两台浏览器互相顶代次；此时只更新
    长寿命凭据，浏览器罐继续持有滚动代次。
    """
    target = _account()
    svc = _mock_service([target])
    auth_file = _auth_file_matching_full_jar(tmp_path, ts_value="server-gen-9")
    svc.get_account_auth_path.return_value = str(auth_file)
    svc.get_active_account.return_value = target
    runtime_state.account_service = svc
    runtime_state.rotator = MagicMock()

    session = MagicMock()
    session._auth_file = str(auth_file)
    session.probe_active_session = AsyncMock(return_value=True)
    session.apply_pushed_cookies = AsyncMock(return_value=True)
    runtime_state.client = MagicMock(_session=session)

    jar = [*_full_jar(), _cookie("__Secure-1PSIDTS", "fresh-gen")]
    async with _client() as client:
        resp = await client.post(
            "/api/accounts/push-cookies",
            json={"cookies": jar, "account_id": "acc_1"},
        )

    assert resp.status_code == 200
    session.probe_active_session.assert_awaited_once_with("0")
    saved = svc.save_account_from_cookies.call_args.kwargs["storage_state"]
    by_name = {c["name"]: c["value"] for c in saved["cookies"]}
    assert by_name["__Secure-1PSIDTS"] == "server-gen-9"
    session.apply_pushed_cookies.assert_awaited_once_with(
        str(auth_file), include_rolling=False
    )


@pytest.mark.asyncio
async def test_push_replaces_session_when_core_credentials_conflict(tmp_path):
    """推送带了不同的 SID/PSID（重新登录/另一份会话）：不能只换核心凭据。"""
    target = _account()
    svc = _mock_service([target])
    auth_file = _rolling_auth_file(
        tmp_path, server_rotated=True, ts_value="server-gen-9"
    )
    svc.get_account_auth_path.return_value = str(auth_file)
    svc.get_active_account.return_value = target
    runtime_state.account_service = svc
    runtime_state.rotator = MagicMock()

    session = MagicMock()
    session._auth_file = str(auth_file)
    session.probe_active_session = AsyncMock(return_value=True)
    session.apply_pushed_cookies = AsyncMock(return_value=True)
    runtime_state.client = MagicMock(_session=session)

    jar = [*_full_jar(), _cookie("__Secure-1PSIDTS", "fresh-gen")]
    async with _client() as client:
        resp = await client.post(
            "/api/accounts/push-cookies",
            json={"cookies": jar, "account_id": "acc_1"},
        )

    assert resp.status_code == 200
    saved = svc.save_account_from_cookies.call_args.kwargs["storage_state"]
    by_name = {c["name"]: c["value"] for c in saved["cookies"]}
    assert by_name["__Secure-1PSIDTS"] == "fresh-gen"
    session.apply_pushed_cookies.assert_awaited_once_with(
        str(auth_file), include_rolling=True
    )


@pytest.mark.asyncio
async def test_push_takes_over_generation_when_server_session_dead(tmp_path):
    """服务端浏览器罐实测已死：即使 expired 标记还没更新，也要接管滚动代次。"""
    target = _account()
    svc = _mock_service([target])
    auth_file = _auth_file_matching_full_jar(tmp_path, ts_value="dead-gen")
    svc.get_account_auth_path.return_value = str(auth_file)
    svc.get_active_account.return_value = target
    runtime_state.account_service = svc
    runtime_state.rotator = MagicMock()

    session = MagicMock()
    session._auth_file = str(auth_file)
    session.probe_active_session = AsyncMock(return_value=False)
    session.apply_pushed_cookies = AsyncMock(return_value=True)
    runtime_state.client = MagicMock(_session=session)

    jar = [*_full_jar(), _cookie("__Secure-1PSIDTS", "fresh-gen")]
    async with _client() as client:
        resp = await client.post(
            "/api/accounts/push-cookies",
            json={"cookies": jar, "account_id": "acc_1"},
        )

    assert resp.status_code == 200
    saved = svc.save_account_from_cookies.call_args.kwargs["storage_state"]
    by_name = {c["name"]: c["value"] for c in saved["cookies"]}
    assert by_name["__Secure-1PSIDTS"] == "fresh-gen"
    session.apply_pushed_cookies.assert_awaited_once_with(
        str(auth_file), include_rolling=True
    )


@pytest.mark.asyncio
async def test_push_jar_with_accounts_domain_cookies_roundtrip():
    """油猴 v1.3.1 起 accounts.google.com 域的 LSID / __Host-*PLSID 随罐推送。

    这些是 RotateCookies 类自愈的原料；推送通道必须原样入库（真实 domain 保留），
    不能因为域是 accounts.google.com 或前缀 __Host- 而丢弃。
    """
    target = _account()
    svc = _mock_service([target])
    runtime_state.account_service = svc
    runtime_state.rotator = MagicMock()

    jar = [
        _cookie("SID", "sid-value"),
        _cookie("SAPISID", "sapisid-value"),
        _cookie("LSID", "lsid-value", domain="accounts.google.com"),
        _cookie("__Host-1PLSID", "plsid-value", domain="accounts.google.com"),
    ]
    async with _client() as client:
        resp = await client.post(
            "/api/accounts/push-cookies",
            json={"cookies": jar, "account_id": "acc_1"},
        )

    assert resp.status_code == 200
    saved = svc.save_account_from_cookies.call_args.kwargs["storage_state"]
    by_name = {(c["name"], c.get("domain")): c["value"] for c in saved["cookies"]}
    assert by_name[("LSID", "accounts.google.com")] == "lsid-value"
    assert by_name[("__Host-1PLSID", "accounts.google.com")] == "plsid-value"
