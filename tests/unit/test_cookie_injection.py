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
    """换账号先清罐：旧账号 Cookie 不得残留，新账号服务会话票照常注入。"""
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
            {
                "name": "SIDCC",
                "value": "disk-b-sidcc",
                "domain": ".google.com",
                "path": "/",
            },
        ],
    )
    page = _page([{"name": "__Secure-3PSIDTS", "value": "jar-a-ts"}])

    assert await load_cookies_to_page(page, auth, clear_first=True) is True
    page.cdp.send.assert_any_call("Network.clearBrowserCookies")
    injected = {c["name"]: c["value"] for c in page.set_cookies.await_args.args[0]}
    # SIDCC 系永远不从磁盘注入；服务会话票随新账号注入
    assert injected == {"SAPISID": "sapi-b", "__Secure-3PSIDTS": "disk-b-ts"}
    assert "__Secure-3PSIDTS" not in page._cached_cookies


@pytest.mark.asyncio
async def test_include_rolling_injects_fresh_generation(tmp_path):
    """RotateCookies 抢救：磁盘刚推进的服务会话票必须覆盖罐里的旧值。"""
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
async def test_cold_start_injects_service_session_ticket(tmp_path):
    """回归：冷启动（空罐）必须注入 PSIDTS 服务会话票，否则被弹回登录页。

    服务器实测（2026-11-02）：把 PSIDTS 一并跳过注入后，浏览器凭完整长寿命
    Cookie 仍被 Google 弹回 accounts.google.com 登录页——服务会话票缺失时
    Google 不会补发。SIDCC 系则必须继续跳过（重放敏感，Google 会重发）。
    """
    auth = _auth_file(
        tmp_path,
        [
            {"name": "SID", "value": "sid", "domain": ".google.com", "path": "/"},
            {
                "name": "__Secure-3PSIDTS",
                "value": "pushed-ts",
                "domain": ".google.com",
                "path": "/",
            },
            {
                "name": "SIDCC",
                "value": "pushed-sidcc",
                "domain": ".google.com",
                "path": "/",
            },
        ],
    )
    page = _page([])

    assert await load_cookies_to_page(page, auth) is True
    injected = {c["name"]: c["value"] for c in page.set_cookies.await_args.args[0]}
    assert injected["__Secure-3PSIDTS"] == "pushed-ts"
    assert "SIDCC" not in injected


@pytest.mark.asyncio
async def test_rolling_only_snapshot_is_noop_when_jar_has_live_values(tmp_path):
    """罐里已有实时代次时，只剩会话票的快照不覆盖罐（热刷新不回滚）。"""
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
    # 注入等价于空操作时不会重建缓存视图，罐本来就是权威
    assert "__Secure-3PSIDTS" not in page._cached_cookies
