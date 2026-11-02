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
async def test_save_browser_cookies_marks_rotated_only_for_new_rolling(tmp_path):
    """浏览器落盘：只有 auth.json 里没有的滚动代次出现时才打 server_rotated。

    每次导航都无条件置位会让标记恒真，推送带来的新代次永远被挡在门外。
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
