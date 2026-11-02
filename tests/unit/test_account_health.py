"""账号登录态巡检与 401 分类测试。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from aistudio_api.application import account_health as health
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
    def __init__(self, status_code: int, set_cookies: list[str] | None = None) -> None:
        self.status_code = status_code
        self.headers = httpx.Headers(
            [("set-cookie", line) for line in (set_cookies or [])]
        )


class _FakeAsyncClient:
    def __init__(
        self,
        status_code: int | None = None,
        exc: Exception | None = None,
        set_cookies: list[str] | None = None,
    ) -> None:
        self._status_code = status_code
        self._exc = exc
        self._set_cookies = set_cookies

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    async def post(self, *args: object, **kwargs: object) -> _FakeResponse:
        if self._exc is not None:
            raise self._exc
        assert self._status_code is not None
        return _FakeResponse(self._status_code, self._set_cookies)


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


def _probe_auth_file(tmp_path: Path) -> Path:
    auth_file = tmp_path / "auth.json"
    auth_file.write_text(
        json.dumps(
            {
                "cookies": [
                    {
                        "name": "SID",
                        "value": "sid",
                        "domain": ".google.com",
                        "path": "/",
                    },
                    {
                        "name": "APISID",
                        "value": "old-apisid",
                        "domain": ".google.com",
                        "path": "/",
                    },
                    {
                        "name": "__Secure-1PSIDTS",
                        "value": "old-ts",
                        "domain": ".google.com",
                        "path": "/",
                    },
                ],
                "origins": [],
            }
        ),
        encoding="utf-8",
    )
    return auth_file


@pytest.mark.asyncio
async def test_probe_persist_does_not_mark_server_rotated_for_long_lived_only(
    tmp_path, monkeypatch
):
    """探活只回写 APISID（长寿凭据）时，不能误判“服务端推进过滚动代次”。

    误置位会让下一次推送带来的新代次被保护逻辑挡住，服务端守着没推进的旧
    代次直到被判过期。
    """
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *args, **kwargs: _FakeAsyncClient(
            200,
            set_cookies=["APISID=new-apisid; Path=/; Domain=.google.com; Secure"],
        ),
    )
    auth_file = _probe_auth_file(tmp_path)

    assert (
        await health.probe_account_session(
            {"SID": "sid", "APISID": "old-apisid"},
            persist_to=auth_file,
        )
        is True
    )

    state = json.loads(auth_file.read_text(encoding="utf-8"))
    by_name = {c["name"]: c["value"] for c in state["cookies"]}
    assert by_name["APISID"] == "new-apisid"
    assert "server_rotated" not in state


@pytest.mark.asyncio
async def test_probe_persist_marks_server_rotated_when_generation_advances(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *args, **kwargs: _FakeAsyncClient(
            200,
            set_cookies=[
                "SIDCC=new-sidcc; Path=/; Domain=.google.com; Secure; HttpOnly"
            ],
        ),
    )
    auth_file = _probe_auth_file(tmp_path)

    await health.probe_account_session(
        {"SID": "sid", "APISID": "old-apisid"},
        persist_to=auth_file,
    )

    state = json.loads(auth_file.read_text(encoding="utf-8"))
    assert state["server_rotated"] is True


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

    probed_with: list[object] = []

    async def fake_probe(
        cookies: dict[str, str], auth_user: str = "0", *, persist_to: object = None
    ):
        probed_with.append(persist_to)
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
    # 探活要带上 auth.json 路径：响应里的 Set-Cookie 向前落盘，闲置账号的滚动链
    # 才会继续推进，不需要人挂着浏览器反复推送
    assert probed_with and all(path is not None for path in probed_with)
    assert ("acc_valid", False) in calls
    assert ("acc_expired", True) in calls
    assert all(account_id != "acc_disabled" for account_id, _ in calls)


# ==================== RotateCookies 主动轮换 ====================


def _rotate_auth_state(tmp_path, *, with_lsid: bool = True) -> Path:
    cookies: list[dict[str, object]] = [
        {"name": "SID", "value": "sid", "domain": ".google.com", "path": "/"},
        {"name": "SAPISID", "value": "sapi", "domain": ".google.com", "path": "/"},
        {
            "name": "__Secure-1PSIDTS",
            "value": "old-ts",
            "domain": ".google.com",
            "path": "/",
        },
    ]
    if with_lsid:
        cookies.append(
            {
                "name": "LSID",
                "value": "lsid-root",
                "domain": "accounts.google.com",
                "path": "/",
            }
        )
        cookies.append(
            {
                "name": "__Host-1PLSID",
                "value": "host-plsid",
                "domain": "accounts.google.com",
                "path": "/",
            }
        )
    auth_file = tmp_path / "auth.json"
    auth_file.write_text(
        json.dumps({"cookies": cookies, "origins": []}), encoding="utf-8"
    )
    return auth_file


@pytest.mark.asyncio
async def test_rotate_account_cookies_success_writes_new_generation(
    tmp_path, monkeypatch
):
    """RotateCookies 成功：新代次落盘 + server_rotated 标记。"""

    class FakeResp:
        status_code = 200

        def __init__(self):
            self.headers = httpx.Headers(
                {
                    "set-cookie": "__Secure-1PSIDTS=sidts-fresh; Path=/; Domain=.google.com; Secure; HttpOnly"
                }
            )

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **kw):
            FakeClient.last_url, FakeClient.last_headers = url, kw.get("headers")
            return FakeResp()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    auth_file = _rotate_auth_state(tmp_path)

    changed = await health.rotate_account_cookies(auth_file)

    assert changed == ["__Secure-1PSIDTS"]
    assert "RotateCookies" in FakeClient.last_url
    sent = FakeClient.last_headers["Cookie"]
    assert "LSID=lsid-root" in sent and "SID=sid" in sent
    state = json.loads(auth_file.read_text(encoding="utf-8"))
    by_name = {c["name"]: c["value"] for c in state["cookies"]}
    assert by_name["__Secure-1PSIDTS"] == "sidts-fresh"
    assert state["server_rotated"] is True


@pytest.mark.asyncio
async def test_rotate_account_cookies_skips_without_lsid(tmp_path, monkeypatch):
    """没有 LSID / __Host-*PLSID 时静默跳过，不发请求（防 Google 判异常）。"""
    called = False

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **kw):
            nonlocal called
            called = True
            raise AssertionError("不应发起请求")

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    auth_file = _rotate_auth_state(tmp_path, with_lsid=False)

    assert await health.rotate_account_cookies(auth_file) == []
    assert called is False


@pytest.mark.asyncio
async def test_rotate_account_cookies_failure_is_silent(tmp_path, monkeypatch):
    """HTTP 非 200 / 网络异常都不抛出、不改文件（保养动作不构成判定）。"""

    class FakeResp:
        status_code = 403
        headers = httpx.Headers({})

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **kw):
            return FakeResp()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    auth_file = _rotate_auth_state(tmp_path)
    before = auth_file.read_text(encoding="utf-8")

    assert await health.rotate_account_cookies(auth_file) == []
    assert auth_file.read_text(encoding="utf-8") == before


def test_rotate_rescue_cooldown_blocks_repeat_within_interval():
    """抢救频控：间隔内同账号不再重复发 RotateCookies（防 Google 风控）。"""
    health._rotate_rescue_at.clear()
    assert health._rotate_rescue_cooldown_ok("acc_a") is True
    assert health._rotate_rescue_cooldown_ok("acc_a") is False
    assert health._rotate_rescue_cooldown_ok("acc_b") is True
    health._rotate_rescue_at.clear()
