"""Tests for the browser cookie push endpoint and normalization helpers."""

from __future__ import annotations

import json
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
) -> AccountMeta:
    return AccountMeta(
        id=account_id,
        name=name,
        email=email,
        created_at="2026-10-17T00:00:00Z",
        last_used=None,
        auth_user=auth_user,
        cookie_id=cookie_id,
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
    async def fake_probe(_cookies, auth_user="0", *, timeout=8.0):
        return True

    monkeypatch.setattr(
        "aistudio_api.application.account_health.probe_account_session",
        fake_probe,
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
            _cookie("SIDCC", "x"),
            _cookie("foo", "x", domain=".example.com"),
            _cookie("bar", "x", domain=""),
        ]
    )
    cookies = state["cookies"]
    assert isinstance(cookies, list)
    assert [c["name"] for c in cookies] == ["SID"]


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
    assert missing_session_cookies(["SAPISID", "__Secure-1PSID"]) == ["SID"]
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
async def test_push_cookies_injects_into_active_browser():
    target = _account()
    svc = _mock_service([target])
    svc.get_active_account.return_value = target
    svc.get_account_auth_path.return_value = "data/accounts/acc_1/auth.json"
    runtime_state.account_service = svc
    runtime_state.rotator = MagicMock()

    session = MagicMock()
    session._auth_file = "data/accounts/acc_1/auth.json"
    session.reload_current_account_cookies = AsyncMock(return_value=True)
    runtime_state.client = MagicMock(_session=session)

    async with _client() as client:
        resp = await client.post(
            "/api/accounts/push-cookies",
            json={"cookies": _full_jar(), "account_id": "acc_1"},
        )

    assert resp.status_code == 200
    assert resp.json()["injected"] is True
    session.reload_current_account_cookies.assert_awaited_once()


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
    async def fake_probe(_cookies, auth_user="0", *, timeout=8.0):
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
    runtime_state.rotator.set_session_expired.assert_any_call("acc_1", False)
    runtime_state.rotator.set_session_expired.assert_any_call("acc_1", True)


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
