"""Account failover and concurrency orchestration."""

from __future__ import annotations

import asyncio
import time

from aistudio_api.api.state import runtime_state
from aistudio_api.domain.errors import SessionExpiredError
from aistudio_api.infrastructure.utils.logger import get_logger

logger = get_logger("orchestrator")
MAX_RETRIES = 5

# 401 原地自愈的最小间隔：同一账号在窗口内只允许被“重导航救一次”，
# 救不回来才判登录态失效，避免持续 401 时反复重导航把整条请求链路拖死。
SELFHEAL_COOLDOWN_S = 180.0

_switch_lock: asyncio.Lock | None = None
_switch_lock_loop: asyncio.AbstractEventLoop | None = None
_last_switch_time: float = 0.0
_selfheal_at: dict[str, float] = {}


def _get_switch_lock() -> asyncio.Lock:
    global _switch_lock, _switch_lock_loop
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if _switch_lock is None or _switch_lock_loop is not loop:
        _switch_lock = asyncio.Lock()
        _switch_lock_loop = loop
    return _switch_lock


async def _try_rotate_cookies_repair(account_id: str) -> bool:
    """401 时的纯 HTTP 抢救：RotateCookies 换新一代代次（pg-bridge 同款）。

    比浏览器重导航快一个量级（几百毫秒 vs 几秒），不占用服务器浏览器；前提是
    auth.json 里有 LSID / __Host-*PLSID 根凭据（油猴 v1.3.2 起随罐推送）。
    躺平策略下这是唯一的主动轮换触发点：平时不碰账号，只在 401 时抢救。
    成功轮换后清掉该账号的滚动代次冷却，让重试立即用上新鲜代次。
    """
    account_service = runtime_state.account_service
    if account_service is None:
        return False
    auth_path = account_service.get_account_auth_path(account_id)
    if not auth_path:
        return False

    from aistudio_api.application.account_health import (
        _rotate_rescue_cooldown_ok,
        rotate_account_cookies,
    )

    if not _rotate_rescue_cooldown_ok(account_id):
        return False
    try:
        changed = await rotate_account_cookies(auth_path)
    except Exception as exc:
        logger.debug("账号 %s RotateCookies 抢救失败: %s", account_id, exc)
        return False
    if not changed:
        return False

    # RotateCookies 只改了 auth.json。当前账号若正在使用，浏览器罐里仍是旧代次，
    # 重试会原样再撞一次 401；必须把新代次灌进页面并立即落盘。这是 401 抢救
    # 允许从磁盘注入滚动代次的入口（include_rolling=True）：此刻 auth.json 刚被
    # RotateCookies 响应推进到最新一代，磁盘才是权威。落盘与注入在同一把锁内，
    # 防止 60 秒定时回写用旧罐覆盖新代次。
    rotator = runtime_state.rotator
    if rotator is not None:
        rotator.set_session_expired(account_id, False)
    client = runtime_state.client
    session = getattr(client, "session", None) if client is not None else None
    active = account_service.get_active_account()
    if (
        session is not None
        and active is not None
        and active.id == account_id
        and session.is_ready
    ):
        reloaded = await session.reload_current_account_cookies(
            include_rolling=True, persist=True
        )
        if not reloaded:
            logger.warning(
                "账号 %s RotateCookies 成功，但新代次未能灌回浏览器，交给后续抢救链",
                account_id,
            )
            return False

    logger.info("账号 %s 已通过 RotateCookies 换新一代代次", account_id)
    return True


async def _try_in_place_session_repair(
    account_id: str,
    model: str | None = None,
) -> bool:
    """让服务端浏览器原地重跑一次会话，尝试自己把登录态救回来。

    Google 的 `*PSIDTS` / `*PSIDCC` 是滚动代次凭据：只要有一次轮换没落盘（另一台
    浏览器也在用这个号、进程重启后读了旧快照），下一次提交的就是上一代，上游直接
    401。这种 401 并不是“密码失效”——重新导航会让 Chromium 吃回 Google 下发的
    新一代 Cookie，请求就能继续。

    只对当前活跃账号有效（切到别的号不算自愈），且受 SELFHEAL_COOLDOWN_S 限流；
    与切号不互斥：切号保请求，这里只在“无处可切”时同步执行，其余情况由巡检
    （每 10 分钟全量探活）兜底仲裁。
    """
    global _last_switch_time

    now = time.time()
    if now - _selfheal_at.get(account_id, 0.0) < SELFHEAL_COOLDOWN_S:
        return False
    _selfheal_at[account_id] = now

    account_service = runtime_state.account_service
    client = runtime_state.client
    rotator = runtime_state.rotator
    if account_service is None or client is None or client.session is None:
        return False
    active = account_service.get_active_account()
    if active is None or active.id != account_id:
        return False

    logger.info(
        "账号 %s 返回 401，先原地刷新会话重试一次（暂不判定登录态失效）", account_id
    )
    client.clear_templates()
    try:
        result = await account_service.activate_account(account_id, client.session)
    except Exception as exc:
        logger.warning("账号 %s 原地刷新会话失败: %s", account_id, exc)
        return False
    if result is None:
        return False

    if rotator is not None:
        rotator.clear_cooldown(account_id, model=model)
    _last_switch_time = time.time()
    logger.info("账号 %s 会话已原地刷新，重试当前请求", account_id)
    return True


async def try_switch_account(
    model: str | None = None,
    failed_account_id: str | None = None,
    required_tiers: tuple[str, ...] | list[str] | None = None,
    *,
    is_session_expired: bool = False,
) -> bool:
    """尝试切换到下一个对目标 model 可用的账号。防止并发级联切号。"""
    global _last_switch_time
    entry_time = time.time()

    if model and not required_tiers:
        from aistudio_api.infrastructure.gateway.model_discovery import model_discovery

        tiers = model_discovery.get_required_tiers(model)
        if tiers:
            required_tiers = tuple(tiers)

    # 乐观快速预检：若无需记录永久失效/短时隔离，且当前活跃账号已被并发请求切换至健康状态，直接免锁复用
    if failed_account_id and not is_session_expired:
        rotator = runtime_state.rotator
        account_service = runtime_state.account_service
        if rotator is not None and account_service is not None:
            cur_active = account_service.get_active_account()
            if (
                cur_active
                and (
                    cur_active.id != failed_account_id
                    or _last_switch_time >= entry_time
                )
                and (not required_tiers or cur_active.tier in required_tiers)
                and rotator.is_account_available(cur_active.id, model=model)
            ):
                return True

    async with _get_switch_lock():
        rotator = runtime_state.rotator
        account_service = runtime_state.account_service
        client = runtime_state.client

        if (
            rotator is None
            or account_service is None
            or client is None
            or client.session is None
        ):
            return False

        current_active = account_service.get_active_account()
        current_id = current_active.id if current_active else None

        # 双重检查 1：如果已有并发协程在 entry_time 之后完成切号或在位自愈刷新，且当前账号对该模型可用，则直接复用
        if (
            _last_switch_time >= entry_time
            and current_id
            and (not is_session_expired or current_id != failed_account_id)
            and (
                not required_tiers
                or (current_active and current_active.tier in required_tiers)
            )
            and rotator.is_account_available(current_id, model=model)
        ):
            logger.info(
                "并发请求已完成切号，复用当前账号: account=%s, model=%s",
                current_id,
                model,
            )
            return True

        # 403 由 api_service_gemini 先原地重签名、再切号；这里不再给账号加冷却锁。
        # 401 的抢救与记账在下方 next_account 分支统一处理（RotateCookies → 重导航 → 记账）。

        # 双重检查 2：如果已经由并发协程切换到了新账号，且新账号对当前 model 可用，则直接复用
        if (
            failed_account_id
            and current_id
            and current_id != failed_account_id
            and (
                not required_tiers
                or (current_active and current_active.tier in required_tiers)
            )
            and rotator.is_account_available(current_id, model=model)
        ):
            logger.info(
                "并发请求已完成切号，复用当前账号: account=%s, model=%s",
                current_id,
                model,
            )
            return True

        next_account = await rotator.get_next_account(
            model=model,
            current_account_id=current_id,
            failed_account_id=failed_account_id,
            required_tiers=required_tiers,
            is_session_expired=is_session_expired,
        )

        if failed_account_id and is_session_expired and next_account is not None:
            # 有备用账号：先切号保请求。切号是同步开销最大的动作，切换前先花几百
            # 毫秒试一次纯 HTTP 抢救（RotateCookies 换新代次）——救回来连号都不用
            # 切，当前请求直接重试。
            if await _try_rotate_cookies_repair(
                failed_account_id
            ) and rotator.is_account_available(failed_account_id, model=model):
                logger.info(
                    "账号 %s 已通过 RotateCookies 复活，无需切换账号", failed_account_id
                )
                return True
            # 401 不一定真失效（滚动代次落后一代也会 401，且账号基本是同一批导入、
            # 经常一起到龄），所以这里只记一个短冷却，交给后台探活仲裁：探活还活着
            # 就自动复活，真死了才会被 `set_session_expired(True)` 拉黑 24 小时。
            rotator.record_auth_error(
                failed_account_id, model=model, cooldown_seconds=300.0
            )
            logger.info(
                "账号 %s 返回 401，先切换到备用账号，登录态交给巡检探活仲裁",
                failed_account_id,
            )

        if next_account is None:
            # 无处可切：抢救链 = 纯 HTTP RotateCookies（快）→ 浏览器重导航自愈（慢）
            if is_session_expired and failed_account_id:
                if await _try_rotate_cookies_repair(failed_account_id) or (
                    await _try_in_place_session_repair(failed_account_id, model=model)
                ):
                    return True
                rotator.record_session_expired(failed_account_id, model=model)
                logger.error(
                    "账号 %s 登录态失效且无其他可用账号，停止重试",
                    failed_account_id,
                )
            else:
                logger.warning(
                    "未找到可用备用账号: model=%s, failed_account=%s",
                    model,
                    failed_account_id,
                )
            return False

        if current_id is None or next_account.id != current_id:
            try:
                result = await account_service.activate_account(
                    next_account.id,
                    client.session,
                )
            except SessionExpiredError as exc:
                # 切换目标本身被 Google 重定向到登录页：这是硬证据，必须记账，
                # 否则账号会一直显示“就绪”，每次被选中都让请求失败一次。
                logger.error(
                    "切换到账号 %s 失败（被重定向到登录页），标记登录态失效: %s",
                    next_account.id,
                    exc,
                )
                rotator.record_session_expired(next_account.id, model=model)
                return False
            except Exception as exc:
                logger.warning("切换到账号 %s 失败: %s", next_account.id, exc)
                return False
            if result is not None:
                rotator.clear_cooldown(next_account.id, model=model)
                _last_switch_time = time.time()
                logger.info(
                    "账号切换成功: %s -> %s, model=%s",
                    current_id,
                    next_account.id,
                    model,
                )
            return result is not None
        # 登录态失效严禁在原账号原地重试
        if is_session_expired and (
            failed_account_id == current_id or next_account.id == failed_account_id
        ):
            logger.error("账号 %s 登录态已失效，禁止在原账号重试", failed_account_id)
            return False

        # 单账号模式或所有其他账号均不可用时（非登录态失效，如偶发 403 鉴权波动），如果指定了 failed_account_id，强制刷新当前会话与 BotGuard
        if failed_account_id and failed_account_id == current_id:
            logger.info(
                "无其他可用备用账号，重置当前账号会话: account=%s",
                current_id,
            )
            client.clear_templates()
            try:
                result = await account_service.activate_account(
                    current_id,
                    client.session,
                )
            except SessionExpiredError as exc:
                logger.error(
                    "账号 %s 刷新后仍被重定向到登录页，标记登录态失效: %s",
                    current_id,
                    exc,
                )
                rotator.record_session_expired(current_id, model=model)
                return False
            except Exception as exc:
                logger.warning("账号 %s 刷新会话失败: %s", current_id, exc)
                return False
            if result is not None:
                rotator.clear_cooldown(current_id, model=model)
                _last_switch_time = time.time()
            return result is not None
        return bool(
            current_id
            and (
                not required_tiers
                or (current_active and current_active.tier in required_tiers)
            )
            and rotator.is_account_available(current_id, model=model)
        )


async def ensure_active_account(attempt: int, model: str | None = None) -> None:
    """确保在初次尝试时存在活跃账号，且若请求付费模型而当前账号权益不足时主动抢切。"""
    if attempt != 0:
        return
    account_svc = runtime_state.account_service
    rotator = runtime_state.rotator
    current = account_svc.get_active_account() if account_svc else None

    # 核心拦截：检测目标模型是否为 Paid 模型且当前账号层级不满足要求
    if model:
        from fastapi import HTTPException

        from aistudio_api.infrastructure.gateway.model_discovery import model_discovery

        if model_discovery.is_paid_model(model):
            req_tier = model_discovery.get_required_tier(model)
            req_tiers = model_discovery.get_required_tiers(model)
            allowed_tiers = (
                set(req_tiers)
                if req_tiers
                else (
                    {"Pro", "Ultra", "Ultra_20X"}
                    if req_tier == "Pro"
                    else {"Ultra", "Ultra_20X"}
                )
            )
            cur_tier = (current.tier if current else None) or "Free"
            if not current or cur_tier not in allowed_tiers:
                logger.info(
                    "模型 %s 需要 %s 权益，当前账号 %s 为 %s，切换至付费账号",
                    model,
                    req_tier,
                    current.id if current else "none",
                    cur_tier,
                )
                switched = await try_switch_account(
                    model=model,
                    required_tiers=tuple(req_tiers)
                    if req_tiers
                    else tuple(allowed_tiers),
                )
                if not switched:
                    logger.warning(
                        "请求模型 %s 需要 %s 付费权益，但账号池中无就绪的可用账号",
                        model,
                        req_tier,
                    )
                    raise HTTPException(
                        status_code=403,
                        detail={
                            "message": f"模型 '{model}' 需要 Google AI Studio {req_tier} 订阅，当前账号池中无就绪的付费账号。",
                            "type": "permission_denied",
                        },
                    )
                return

    if not current:
        await try_switch_account(model=model)
    elif (
        rotator and model and not rotator.is_account_available(current.id, model=model)
    ):
        await try_switch_account(model=model, failed_account_id=current.id)


def record_rotator_event(
    event: str,
    model: str | None = None,
    usage: dict[str, object] | None = None,
) -> None:
    """记录调度器事件（成功、限流、错误、鉴权异常）。"""
    rotator = runtime_state.rotator
    account_service = runtime_state.account_service
    account = account_service.get_active_account() if account_service else None
    if not rotator or account is None:
        return
    if event == "success":
        rotator.record_success(account.id, model=model, usage=usage)
    elif event == "rate_limited":
        rotator.record_rate_limited(account.id, model=model)
    elif event == "auth_error":
        rotator.record_auth_error(account.id, model=model)
    elif event == "error":
        rotator.record_error(account.id, model=model)


__all__ = [
    "MAX_RETRIES",
    "ensure_active_account",
    "record_rotator_event",
    "try_switch_account",
]
