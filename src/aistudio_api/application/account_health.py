"""账号登录态巡检与浏览器会话保活。"""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING, TypedDict

from aistudio_api.api.state import runtime_state
from aistudio_api.config import settings
from aistudio_api.domain.errors import SessionExpiredError
from aistudio_api.infrastructure.account.cookie_parser import (
    DEFAULT_API_KEY,
    DEFAULT_EXT_BIN,
    DEFAULT_ORIGIN,
    DEFAULT_USER_AGENT,
    calculate_sapisid_hash,
    parse_raw_cookies,
)
from aistudio_api.infrastructure.utils.logger import get_logger

if TYPE_CHECKING:
    from aistudio_api.application.account_rotator import AccountRotator
    from aistudio_api.application.account_service import AccountService

logger = get_logger("account_health")


class AccountHealthSummary(TypedDict):
    checked: int
    valid: int
    expired: list[str]
    unknown: list[str]


_LIST_MODELS_URL = (
    "https://alkalimakersuite-pa.clients6.google.com/$rpc/"
    "google.internal.alkali.applications.makersuite.v1.MakerSuiteService/ListModels"
)


async def probe_account_session(
    cookies: dict[str, str],
    auth_user: str = "0",
    *,
    timeout: float = 8.0,
) -> bool | None:
    """用账号 Cookie 请求 ListModels，判断登录态是否仍有效。

    Returns:
        True 有效；False 上游明确返回 401；None 网络异常或未知状态。
    """
    if not cookies:
        return False

    import httpx

    headers = {
        "Content-Type": "application/json+protobuf",
        "X-User-Agent": DEFAULT_USER_AGENT,
        "X-Goog-Api-Key": DEFAULT_API_KEY,
        "X-Goog-AuthUser": str(auth_user or "0"),
        "X-Goog-Ext-519733851-bin": DEFAULT_EXT_BIN,
        "X-AIStudio-Visit-Id": f"v1_{uuid.uuid4()}",
        "Origin": DEFAULT_ORIGIN,
        "Referer": f"{DEFAULT_ORIGIN}/",
        "Cookie": "; ".join(f"{name}={value}" for name, value in cookies.items()),
    }
    auth_header = calculate_sapisid_hash(cookies)
    if auth_header:
        headers["Authorization"] = auth_header

    try:
        async with httpx.AsyncClient(
            timeout=timeout, proxy=settings.proxy_url
        ) as client:
            response = await client.post(
                _LIST_MODELS_URL, headers=headers, content="[]"
            )
    except Exception as exc:
        logger.debug("账号探活请求失败: %s", exc)
        return None

    if response.status_code in (200, 400, 403):
        # 400/403 说明请求已经通过 Google 鉴权，只是参数或权限受限
        return True
    if response.status_code == 401:
        return False
    logger.debug("账号探活返回未知状态: HTTP %s", response.status_code)
    return None


async def check_all_accounts(
    account_service: AccountService,
    rotator: AccountRotator,
    *,
    concurrency: int = 4,
) -> AccountHealthSummary:
    """巡检所有启用账号并同步登录态到调度器。"""
    accounts = [
        account for account in account_service.list_accounts() if account.enabled
    ]
    active = account_service.get_active_account()
    active_id = active.id if active is not None else None

    # 活跃账号以浏览器实时 Cookie 为准，避免用 60 秒前的快照误判
    client = runtime_state.client
    active_ready = True
    if client is not None and client.session is not None:
        if client.session.is_ready:
            try:
                active_ready = await client.session.save_active_cookies(force=True)
            except Exception as exc:
                logger.debug("保存活跃账号实时 Cookie 失败: %s", exc)
                active_ready = False
        else:
            active_ready = False

    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def _probe(account_id: str, auth_user: str) -> bool | None:
        async with semaphore:
            if account_id == active_id and not active_ready:
                # 浏览器尚未就绪时，auth.json 可能是旧快照，不做失效判定
                return None
            path = account_service.get_auth_path(account_id)
            if path is None:
                return None
            try:
                cookies = parse_raw_cookies(path.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.debug("读取账号 %s 的 Cookie 失败: %s", account_id, exc)
                return None
            return await probe_account_session(cookies, auth_user)

    results = await asyncio.gather(
        *(_probe(account.id, account.auth_user) for account in accounts)
    )

    valid = 0
    expired: list[str] = []
    unknown: list[str] = []
    for account, status in zip(accounts, results, strict=False):
        if status is True:
            valid += 1
            rotator.set_session_expired(account.id, False)
        elif status is False:
            expired.append(account.id)
            rotator.set_session_expired(account.id, True)
        else:
            unknown.append(account.id)

    return {
        "checked": len(accounts),
        "valid": valid,
        "expired": expired,
        "unknown": unknown,
    }


async def keepalive_active_session() -> None:
    """空闲时在浏览器内重新加载 AI Studio，触发 Cookie 轮换并验证登录态。"""
    client = runtime_state.client
    account_service = runtime_state.account_service
    rotator = runtime_state.rotator
    if client is None or client.session is None or account_service is None:
        return
    if rotator is None:
        return

    active = account_service.get_active_account()
    if active is None:
        return
    session = client.session
    if not session.is_ready or session.in_flight > 0:
        return
    if not rotator.is_account_available(active.id):
        return

    try:
        await session.ensure_botguard_service(force_refresh=True)
        logger.info("账号 %s 会话保活完成，Cookie 已刷新", active.id)
    except SessionExpiredError as exc:
        rotator.set_session_expired(active.id, True)
        logger.error("账号 %s 会话保活失败，登录态已失效: %s", active.id, exc)
    except Exception as exc:
        logger.warning("账号 %s 会话保活失败，稍后重试: %s", active.id, exc)
