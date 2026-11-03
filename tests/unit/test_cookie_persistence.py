"""Unit tests for runtime cookie writeback."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aistudio_api.infrastructure.browser.cdp_client import CDPPage
from aistudio_api.infrastructure.gateway.session import BrowserSession


def _auth_cookies(value: str = "sapisid") -> list[dict[str, object]]:
    return [
        {
            "name": "SAPISID",
            "value": value,
            "domain": ".google.com",
            "path": "/",
            "expires": 1800000000,
        },
        {
            "name": "__Secure-1PSID",
            "value": "psid",
            "domain": ".google.com",
            "path": "/",
            "expires": 1800000000,
        },
    ]


def _page_with_cookies(cookies: list[dict[str, object]]) -> MagicMock:
    page = MagicMock(spec=CDPPage)
    page.is_closed = MagicMock(return_value=False)
    page.get_cookies = AsyncMock(return_value=cookies)
    return page


@pytest.mark.asyncio
async def test_save_cookies_writes_once_then_skips_unchanged(tmp_path):
    auth_dir = tmp_path / "acc"
    auth_dir.mkdir()
    auth_file = auth_dir / "auth.json"
    page = _page_with_cookies(_auth_cookies())
    session = BrowserSession(port=9222)

    with patch(
        "aistudio_api.infrastructure.gateway.session.save_browser_cookies",
        AsyncMock(return_value=True),
    ) as save:
        assert await session._save_cookies(page=page, auth_file=str(auth_file))
        assert not await session._save_cookies(page=page, auth_file=str(auth_file))
        assert save.await_count == 1

        changed = _page_with_cookies(_auth_cookies("rotated"))
        assert await session._save_cookies(page=changed, auth_file=str(auth_file))
        assert save.await_count == 2

        assert await session._save_cookies(
            page=changed, auth_file=str(auth_file), force=True
        )
        assert save.await_count == 3


@pytest.mark.asyncio
async def test_save_cookies_skips_logged_out_jar(tmp_path):
    auth_dir = tmp_path / "acc"
    auth_dir.mkdir()
    auth_file = auth_dir / "auth.json"
    page = _page_with_cookies(
        [{"name": "NID", "value": "anonymous", "domain": ".google.com", "path": "/"}]
    )
    session = BrowserSession(port=9222)

    with patch(
        "aistudio_api.infrastructure.gateway.session.save_browser_cookies",
        AsyncMock(return_value=True),
    ) as save:
        assert not await session._save_cookies(page=page, auth_file=str(auth_file))
        save.assert_not_awaited()


@pytest.mark.asyncio
async def test_save_cookies_skips_missing_account_directory(tmp_path):
    auth_file = tmp_path / "deleted" / "auth.json"
    page = _page_with_cookies(_auth_cookies())
    session = BrowserSession(port=9222)

    with patch(
        "aistudio_api.infrastructure.gateway.session.save_browser_cookies",
        AsyncMock(return_value=True),
    ) as save:
        assert not await session._save_cookies(page=page, auth_file=str(auth_file))
        save.assert_not_awaited()


@pytest.mark.asyncio
async def test_save_browser_cookies_marks_rotated_on_service_ticket_advance(tmp_path):
    """浏览器落盘：服务会话票的值发生变化（同名换值）时打 server_rotated。

    每次导航都无条件置位会让标记恒真；反过来，只在“新名字首次出现”时置位
    会让 PSIDTS 每次同名换值都看不到推进。SIDCC 系是全局噪声，不算推进。
    """
    import json

    from aistudio_api.infrastructure.gateway.session_auth import (
        save_browser_cookies,
    )

    auth_file = tmp_path / "auth.json"
    auth_file.write_text(
        json.dumps(
            {
                "cookies": [
                    {
                        "name": "SIDCC",
                        "value": "old-sidcc",
                        "domain": ".google.com",
                        "path": "/",
                    }
                ],
                "origins": [],
                "server_rotated": True,
            }
        ),
        encoding="utf-8",
    )
    page = _page_with_cookies(
        [
            {"name": "SAPISID", "value": "sapi", "domain": ".google.com", "path": "/"},
            {
                "name": "SIDCC",
                "value": "same-sidcc",
                "domain": ".google.com",
                "path": "/",
            },
        ]
    )

    assert await save_browser_cookies(page, str(auth_file))
    state = json.loads(auth_file.read_text(encoding="utf-8"))
    # 只有 SIDCC 变化：不构成服务会话推进
    assert "server_rotated" not in state

    page = _page_with_cookies(
        [
            {"name": "SAPISID", "value": "sapi", "domain": ".google.com", "path": "/"},
            {
                "name": "__Secure-1PSIDTS",
                "value": "new-ts",
                "domain": ".google.com",
                "path": "/",
            },
        ]
    )
    assert await save_browser_cookies(page, str(auth_file))
    state = json.loads(auth_file.read_text(encoding="utf-8"))
    assert state["server_rotated"] is True

    page = _page_with_cookies(
        [
            {"name": "SAPISID", "value": "sapi", "domain": ".google.com", "path": "/"},
            {
                "name": "__Secure-1PSIDTS",
                "value": "newer-ts",
                "domain": ".google.com",
                "path": "/",
            },
        ]
    )
    assert await save_browser_cookies(page, str(auth_file))
    state = json.loads(auth_file.read_text(encoding="utf-8"))
    assert state["server_rotated"] is True


@pytest.mark.asyncio
async def test_save_browser_cookies_preserves_accounts_root_cookies(tmp_path):
    """浏览器罐看不到 accounts.google.com 的 host-only 根凭据，回写时必须保留。

    这些 Cookie 是 RotateCookies 401 抢救的原料；AI Studio 页面的
    Network.getCookies 不会返回它们，直接覆盖 auth.json 会让抢救永远失效。
    """
    import json
    import time

    from aistudio_api.infrastructure.gateway.session_auth import (
        save_browser_cookies,
    )

    now = int(time.time())
    auth_file = tmp_path / "auth.json"
    auth_file.write_text(
        json.dumps(
            {
                "cookies": [
                    {
                        "name": "LSID",
                        "value": "lsid-root",
                        "domain": "accounts.google.com",
                        "path": "/",
                        "expires": now + 86400,
                    },
                    {
                        "name": "__Host-1PLSID",
                        "value": "plsid-root",
                        "domain": "accounts.google.com",
                        "path": "/",
                        "expires": -1,
                    },
                    {
                        "name": "__Host-GAPS",
                        "value": "expired-root",
                        "domain": "accounts.google.com",
                        "path": "/",
                        "expires": now - 60,
                    },
                    {
                        "name": "SIDCC",
                        "value": "old-sidcc",
                        "domain": ".google.com",
                        "path": "/",
                    },
                ],
                "origins": [],
            }
        ),
        encoding="utf-8",
    )
    page = _page_with_cookies(
        [
            {"name": "SAPISID", "value": "sapi", "domain": ".google.com", "path": "/"},
            {
                "name": "SIDCC",
                "value": "new-sidcc",
                "domain": ".google.com",
                "path": "/",
            },
        ]
    )

    assert await save_browser_cookies(page, str(auth_file))
    state = json.loads(auth_file.read_text(encoding="utf-8"))
    by_name = {str(c["name"]): c for c in state["cookies"]}
    assert by_name["LSID"]["value"] == "lsid-root"
    assert by_name["__Host-1PLSID"]["value"] == "plsid-root"
    assert "__Host-GAPS" not in by_name
    assert by_name["SIDCC"]["value"] == "new-sidcc"


@pytest.mark.asyncio
async def test_apply_pushed_cookies_injects_then_saves():
    """推送注入必须紧接着在锁内落盘，不能留给 60 秒回写用旧罐覆盖。"""
    from types import SimpleNamespace

    session = BrowserSession(port=9222)
    page = MagicMock(spec=CDPPage)
    page.is_closed = MagicMock(return_value=False)
    session._gate = MagicMock(active_context=SimpleNamespace(page=page))

    with (
        patch(
            "aistudio_api.infrastructure.gateway.session.load_cookies_to_page",
            AsyncMock(return_value=True),
        ) as inject,
        patch.object(session, "_save_cookies", AsyncMock(return_value=True)) as save,
    ):
        assert await session.apply_pushed_cookies(
            "data/accounts/acc_1/auth.json", include_rolling=True
        )

    inject.assert_awaited_once_with(
        page, "data/accounts/acc_1/auth.json", include_rolling=True
    )
    save.assert_awaited_once()
    assert save.await_args.kwargs["auth_file"] == "data/accounts/acc_1/auth.json"
    assert save.await_args.kwargs["force"] is True
    assert save.await_args.kwargs["_lock_held"] is True


@pytest.mark.asyncio
async def test_apply_pushed_cookies_skips_save_when_injection_fails():
    from types import SimpleNamespace

    session = BrowserSession(port=9222)
    page = MagicMock(spec=CDPPage)
    page.is_closed = MagicMock(return_value=False)
    session._gate = MagicMock(active_context=SimpleNamespace(page=page))

    with (
        patch(
            "aistudio_api.infrastructure.gateway.session.load_cookies_to_page",
            AsyncMock(return_value=False),
        ),
        patch.object(session, "_save_cookies", AsyncMock(return_value=True)) as save,
    ):
        assert not await session.apply_pushed_cookies(
            "data/accounts/acc_1/auth.json", include_rolling=False
        )

    save.assert_not_awaited()


@pytest.mark.asyncio
async def test_reload_with_persist_serializes_injection_and_save():
    """RotateCookies 抢救路径也要注入后立即落盘，避免被定时回写覆盖。"""
    from types import SimpleNamespace

    session = BrowserSession(port=9222)
    session._auth_file = "data/accounts/acc_1/auth.json"
    page = MagicMock(spec=CDPPage)
    page.is_closed = MagicMock(return_value=False)
    session._gate = MagicMock(active_context=SimpleNamespace(page=page))

    with (
        patch(
            "aistudio_api.infrastructure.gateway.session.load_cookies_to_page",
            AsyncMock(return_value=True),
        ) as inject,
        patch.object(session, "_save_cookies", AsyncMock(return_value=True)) as save,
    ):
        assert await session.reload_current_account_cookies(
            include_rolling=True, persist=True
        )

    inject.assert_awaited_once_with(
        page, "data/accounts/acc_1/auth.json", include_rolling=True
    )
    save.assert_awaited_once()
    assert save.await_args.kwargs["_lock_held"] is True
