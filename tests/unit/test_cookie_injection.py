"""Unit tests for load_cookies_to_page cookie-ownership rules.

滚动代次（SIDCC / *PSIDTS / *PSIDRTS / *PSIDCC / __Olefcr-*）的权威来源是浏览器罐；
auth.json 只是镜像。注入规则：
- 默认：不清罐、不注入滚动代次，长寿命凭据照常覆盖；
- clear_first=True：换账号，先清罐再注入，滚动代次同样不注入；
- include_rolling=True：仅 RotateCookies 抢救，把磁盘上的最新代次灌进罐，
  且不得把注入前罐里的旧代次写回 _cached_cookies。
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from aistudio_api.infrastructure.gateway.session_auth import load_cookies_to_page


def _auth_file(tmp_path: Path, cookies: list[dict[str, object]]) -> str:
    path = tmp_path / "auth.json"
    path.write_text(
        json.dumps({"cookies": cookies, "origins": []}), encoding="utf-8"
    )
    return str(path)


def _page(live_cookies: list[dict[str, object]]) -> MagicMock:
    page = MagicMock()
    page.get_cookies = AsyncMock(return_value=live_cookies)
    page.set_cookies = AsyncMock()
    page.cdp.send = AsyncMock()
    page._cached_cookies = {}
    return page


@pytest.mark.asyncio
async def test_default_injection_keeps_jar_rolling(tmp_path):
    """默认不注入滚动代次，罐里的实时代次保留并补回缓存视图。"""
    auth = _auth_file(
        tmp_path,
        [
            {"name": "SAPISID", "value": "sapi", "domain": ".google.com", "path": "/"},
            {
                "name": "__Secure-3PSIDTS",
                "value": "disk-old-ts",
                "domain": ".google.com",
                "path": "/",
            },
        ],
    )
    page = _page(
        [{"name": "__Secure-3PSIDTS", "value": "jar-fresh-ts"}]
    )

    assert await load_cookies_to_page(page, auth) is True
    injected = {c["name"]: c["value"] for c in page.set_cookies.await_args.args[0]}
    assert injected == {"SAPISID": "sapi"}
    assert page._cached_cookies["__Secure-3PSIDTS"] == "jar-fresh-ts"
    page.cdp.send.assert_not_called()


@pytest.mark.asyncio
async def test_clear_first_resets_jar_for_account_switch(tmp_path):
    """换账号先清罐：旧账号滚动代次不得残留在罐或缓存里。"""
    auth = _auth_file(
        tmp_path,
        [
            {"name": "SAPISID", "value": "sapi-b", "domain": ".google.com", "path": "/"},
            {
                "name": "__Secure-3PSIDTS",
                "value": "disk-b-ts",
                "domain": ".google.com",
                "path": "/",
            },
        ],
    )
    page = _page([{"name": "__Secure-3PSIDTS", "value": "jar-a-ts"}])

    assert await load_cookies_to_page(page, auth, clear_first=True) is True
    page.cdp.send.assert_any_call("Network.clearBrowserCookies")
    injected = {c["name"]: c["value"] for c in page.set_cookies.await_args.args[0]}
    assert injected == {"SAPISID": "sapi-b"}
    assert "__Secure-3PSIDTS" not in page._cached_cookies


@pytest.mark.asyncio
async def test_include_rolling_injects_fresh_generation(tmp_path):
    """RotateCookies 抢救：磁盘刚推进的滚动代次必须进罐，且不回滚缓存。"""
    auth = _auth_file(
        tmp_path,
        [
            {"name": "SAPISID", "value": "sapi", "domain": ".google.com", "path": "/"},
            {
                "name": "__Secure-3PSIDTS",
                "value": "rotated-new-ts",
                "domain": ".google.com",
                "path": "/",
            },
        ],
    )
    page = _page([{"name": "__Secure-3PSIDTS", "value": "jar-stale-ts"}])

    assert await load_cookies_to_page(page, auth, include_rolling=True) is True
    injected = {c["name"]: c["value"] for c in page.set_cookies.await_args.args[0]}
    assert injected["__Secure-3PSIDTS"] == "rotated-new-ts"
    assert page._cached_cookies.get("__Secure-3PSIDTS") != "jar-stale-ts"


@pytest.mark.asyncio
async def test_rolling_only_snapshot_is_noop_when_jar_has_live_values(tmp_path):
    """auth.json 只剩滚动代次且罐里已有实时值：注入等价于空操作，会话仍可用。"""
    auth = _auth_file(
        tmp_path,
        [
            {
                "name": "__Secure-3PSIDTS",
                "value": "disk-old-ts",
                "domain": ".google.com",
                "path": "/",
            }
        ],
    )
    page = _page([{"name": "__Secure-3PSIDTS", "value": "jar-fresh-ts"}])

    assert await load_cookies_to_page(page, auth) is True
    page.set_cookies.assert_not_called()


@pytest.mark.asyncio
async def test_rolling_only_snapshot_fails_when_jar_empty(tmp_path):
    """罐里没有任何实时凭据时，只剩滚动代次的快照救不了会话。"""
    auth = _auth_file(
        tmp_path,
        [
            {
                "name": "__Secure-3PSIDTS",
                "value": "disk-old-ts",
                "domain": ".google.com",
                "path": "/",
            }
        ],
    )
    page = _page([])

    assert await load_cookies_to_page(page, auth) is False
    page.set_cookies.assert_not_called()
