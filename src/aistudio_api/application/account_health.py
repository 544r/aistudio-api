"""账号登录态巡检与浏览器会话保活。"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

from aistudio_api.api.state import runtime_state
from aistudio_api.config import settings
from aistudio_api.domain.errors import SessionExpiredError
from aistudio_api.infrastructure.account.cookie_parser import (
    DEFAULT_API_KEY,
    DEFAULT_EXT_BIN,
    DEFAULT_ORIGIN,
    DEFAULT_USER_AGENT,
    SERVICE_SESSION_COOKIE_NAMES,
    apply_set_cookie_lines,
    calculate_sapisid_hash,
    parse_raw_cookies,
)
from aistudio_api.infrastructure.utils.common import atomic_write_json
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

_ROTATE_COOKIES_URL = "https://accounts.google.com/RotateCookies"
_ROTATE_COOKIES_BODY = '[null, "-1234567890", 0]'
_ROTATE_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)
# RotateCookies 的前置条件：accounts.google.com 的 host-only 会话根凭据
# （油猴 v1.3.2 起随罐推送入库）。缺了它们硬调会被 Google 判异常，直接跳过。
_ROTATE_REQUIRED_NAMES = ("LSID", "__Host-1PLSID", "__Host-3PLSID")


def _build_rotate_cookie_header(state: dict[str, object]) -> tuple[str, str] | None:
    """从 auth.json 里提取 RotateCookies 需要的 Cookie 头。

    返回 (header, anchor_name)；没有 LSID / __Host-*PLSID 任一返回 None。
    accounts.google.com 的 host-only Cookie 只随该域发送，这里原样拼全量头。
    """
    cookies: list[dict[str, object]] = (
        state.get("cookies") if isinstance(state.get("cookies"), list) else []
    )
    by_name: dict[str, str] = {}
    for item in cookies:
        if isinstance(item, dict) and item.get("name"):
            by_name.setdefault(str(item["name"]), str(item.get("value") or ""))
    anchor = next((n for n in _ROTATE_REQUIRED_NAMES if by_name.get(n)), None)
    if anchor is None:
        return None
    if not any(by_name.get(n) for n in ("SID", "__Secure-1PSID", "__Secure-3PSID")):
        return None
    # 只带 google.com 系的键值，避免把无关域的 Cookie 串进请求头
    header = "; ".join(
        f"{name}={value}"
        for name, value in by_name.items()
        if value
        and (
            name.startswith(("__Secure-", "__Host-"))
            or name
            in ("SID", "HSID", "SSID", "APISID", "SAPISID", "LSID", "ACCOUNT_CHOOSER")
        )
    )
    return (header, anchor) if header else None


async def rotate_account_cookies(auth_path: str | Path) -> list[str]:
    """对一个账号执行 RotateCookies 主动轮换（pg-bridge 401 自愈的定时版）。

    用 accounts.google.com 的长寿根凭据（LSID / __Host-*PLSID）向 Google 换一套
    新的滚动代次（PSIDTS / SIDCC 等），并把 Set-Cookie 应用回 auth.json。给闲置
    账号用的——它们没有浏览器保活，探活又基本不带轮换头，躺着时只有这条路能把
    代次链续上，被切回来的瞬间就是新鲜的。

    失败一律静默返回 []：这只是保养动作，不构成任何登录态判定。
    """
    target = Path(auth_path)
    if not target.is_file():
        return []
    try:
        state = json.loads(target.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.debug("读取 %s 做 RotateCookies 失败: %s", target.name, exc)
        return []
    if not isinstance(state, dict):
        return []

    built = _build_rotate_cookie_header(state)
    if built is None:
        return []
    header, anchor = built

    import httpx

    try:
        async with httpx.AsyncClient(timeout=10.0, proxy=settings.proxy_url) as client:
            resp = await client.post(
                _ROTATE_COOKIES_URL,
                headers={
                    "Cookie": header,
                    "User-Agent": _ROTATE_USER_AGENT,
                    "Content-Type": "application/json",
                    "Origin": "https://accounts.google.com",
                    "Referer": "https://accounts.google.com/RotateCookiesPage",
                },
                content=_ROTATE_COOKIES_BODY,
            )
    except Exception as exc:
        logger.debug("账号 %s RotateCookies 请求失败: %s", target.name, exc)
        return []

    if resp.status_code != 200:
        logger.debug(
            "账号 %s RotateCookies 返回 HTTP %s（凭据根=%s）",
            target.name,
            resp.status_code,
            anchor,
        )
        return []

    changed = _persist_rotated_cookies(target, resp.headers.get_list("set-cookie"))
    if changed:
        logger.info(
            "RotateCookies 已推进 %s 的会话 Cookie: %s",
            target.name,
            ", ".join(sorted(set(changed))),
        )
        _record_rotation_event(
            target,
            source="rotate",
            reason="RotateCookies 401 抢救",
            changed_names=changed,
        )
    return changed


# 躺平策略：不做定时轮换，RotateCookies 只在 401 抢救时被动触发（见
# account_orchestrator._try_rotate_cookies_repair）。保留每账号的抢救记账，
# 防止持续 401 的账号每次重试都打 RotateCookies 触发 Google 频控。
_ROTATE_RESCUE_MIN_INTERVAL_S = 300.0

# 进程内抢救记账：account_id -> 上次 RotateCookies 尝试的 time.time()
_rotate_rescue_at: dict[str, float] = {}


def _rotate_rescue_cooldown_ok(account_id: str) -> bool:
    """同一账号 5 分钟内只允许一次 RotateCookies 抢救尝试。"""
    last = _rotate_rescue_at.get(account_id, 0.0)
    ok = time.time() - last >= _ROTATE_RESCUE_MIN_INTERVAL_S
    if ok:
        _rotate_rescue_at[account_id] = time.time()
    return ok


def _record_rotation_event(
    auth_path: str | Path,
    *,
    source: str,
    reason: str,
    changed_names: list[str] | None = None,
) -> None:
    """把 auth.json 的代次推进记入轮换流水（键名+短指纹，不含值）。

    `changed_names` 是本次推进实际涉及的键（调用方从 apply_set_cookie_lines
    拿得到）。不给时回退为全键快照对比（基线为内存里的上次事件，首次为空）。
    两种方式都只产出 8 位短指纹。
    """
    from aistudio_api.application.cookie_rotation_history import (
        RotationChange,
        diff_cookie_values_as_changes,
        get_rotation_history,
        short_digest,
        snapshot_cookie_values,
    )

    with suppress(Exception):
        target = Path(auth_path)
        state = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            return
        cookies = state.get("cookies")
        snap = snapshot_cookie_values(cookies)
        if changed_names is not None:
            changes = [
                RotationChange(name=name, domain="", after=short_digest(snap.get(name)))
                for name in sorted(set(changed_names))
                if snap.get(name)
            ]
        else:
            changes = diff_cookie_values_as_changes(None, snap)
        get_rotation_history().record(
            account_id=target.parent.name,
            source=source,
            kind="rotated",
            reason=reason,
            cookie_count=len(cookies) if isinstance(cookies, list) else 0,
            changes=changes,
        )


def _persist_rotated_cookies(auth_path: str | Path, lines: list[str]) -> list[str]:
    """把上游 `Set-Cookie` 向前合并进账号的 auth.json。

    只写入、不改判：任何异常都吞掉，探活结论与它无关。
    """
    if not lines:
        return []
    target = Path(auth_path)
    if not target.is_file():
        return []
    try:
        state = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            return []
        merged, changed = apply_set_cookie_lines(state, lines)
        if not changed:
            return []
        # 标记“这份 auth.json 的服务级会话票由服务端会话推进过”：推送合并时据此
        # 保护这份代次不被另一台浏览器的旧代次顶回（见 routes_accounts）。
        #
        # 只认 PSIDTS/PSIDRTS/Olefcr 服务会话票的推进。SIDCC 系 Google 对任何
        # 响应（包括未登录请求）都会重发，探活推进 SIDCC 不构成服务会话推进，
        # 误打标记会把推送带来的新鲜会话票挡在外面，服务端守着旧代次直到过期。
        if any(name in SERVICE_SESSION_COOKIE_NAMES for name in changed):
            merged["server_rotated"] = True
        atomic_write_json(target, merged)
    except Exception as exc:
        logger.debug("回写探活轮换 Cookie 失败 (%s): %s", target, exc)
        return []
    logger.info(
        "探活响应已推进 %s 的会话 Cookie: %s",
        target.name,
        ", ".join(sorted(set(changed))),
    )
    _record_rotation_event(
        target,
        source="probe",
        reason="探活响应 Set-Cookie 回写",
        changed_names=changed,
    )
    return changed


async def probe_account_session(
    cookies: dict[str, str],
    auth_user: str = "0",
    *,
    timeout: float = 8.0,
    persist_to: str | Path | None = None,
) -> bool | None:
    """用账号 Cookie 请求 ListModels，判断登录态是否仍有效。

    传入 `persist_to`（账号 auth.json 路径）时，会把响应头里 Google 下发的
    `Set-Cookie` 合并回该文件。这一步是无人值守续命的关键：ListModels 探活本来
    就每 10 分钟对全部启用账号跑一次，顺手把响应里的新一代 PSIDTS / SIDCC 落盘，
    闲置账号的滚动链也在往前走，就不需要靠人挂着浏览器反复推送了。

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

    if persist_to is not None:
        _persist_rotated_cookies(persist_to, response.headers.get_list("set-cookie"))

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
            return await probe_account_session(cookies, auth_user, persist_to=path)

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
