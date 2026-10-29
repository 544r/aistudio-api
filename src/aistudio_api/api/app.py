"""FastAPI application entrypoint."""

from __future__ import annotations

import argparse
import asyncio
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from aistudio_api.application.api_service_gemini import classify_gemini_error_payload
from aistudio_api.config import settings
from aistudio_api.infrastructure.gateway.client import AIStudioClient
from aistudio_api.infrastructure.gateway.model_defaults import (
    get_configured_logging_settings,
)
from aistudio_api.infrastructure.utils.logger import (
    dump_request_exchange,
    get_logger,
    is_dump_requests_enabled,
    setup_logging,
)

from .dependencies import require_api_key, require_web_auth
from .routes_accounts import router as accounts_router
from .routes_anthropic import router as anthropic_router
from .routes_gemini import router as gemini_router
from .routes_models import router as models_router
from .routes_openai import router as openai_router
from .routes_system import (
    protected_router as system_protected_router,
    public_router as system_public_router,
)
from .state import runtime_state

_log_cfg = get_configured_logging_settings()
setup_logging(str(_log_cfg.get("level", "INFO")))
logger = get_logger("server")

# Dump 会把请求头与查询参数原样落盘；管理密码、API Key 与 Cookie 会话票
# 不应出现在可下载的调试文件里，统一打码。
_DUMP_SENSITIVE_HEADERS = frozenset(
    {"authorization", "x-api-key", "x-goog-api-key", "cookie", "set-cookie"}
)
_DUMP_SENSITIVE_QUERY_KEYS = frozenset({"key", "password", "token", "api_key"})
_DUMP_REDACTED = "***"


def _mask_dump_value(key: str, value: str, sensitive: frozenset[str]) -> str:
    return _DUMP_REDACTED if key.lower() in sensitive else value


def _masked_dump_url(url: str, query: dict[str, str]) -> str:
    """用已打码的查询参数字典重建 URL，避免原值随 url 字段落盘。"""
    if "?" not in url:
        return url
    base = url.split("?", 1)[0]
    masked = urlencode(query)
    return f"{base}?{masked}" if masked else base


@asynccontextmanager
async def lifespan(_app: FastAPI):
    from aistudio_api.application.account_rotator import init_rotator
    from aistudio_api.application.account_service import AccountService
    from aistudio_api.infrastructure.account.account_store import AccountStore
    from aistudio_api.infrastructure.browser.browser_engine import (
        install_process_cleanup_handlers,
    )

    install_process_cleanup_handlers()

    client = AIStudioClient(
        port=runtime_state.browser_port,
    )
    runtime_state.client = client
    client.start_cookie_persistence()

    # 初始化账号管理服务
    account_store = AccountStore()
    account_service = AccountService(account_store)
    runtime_state.account_service = account_service

    # 初始化黏性账号调度器
    rotator = init_rotator(account_store)
    runtime_state.rotator = rotator

    logger.info(
        "Client initialized (port=%s, accounts=%d)",
        runtime_state.browser_port,
        len(account_store.list_accounts()),
    )

    # 后台预热浏览器，避免首次请求延迟
    warmup_task = None

    async def _warmup():
        for attempt in range(3):
            try:
                await client.warmup()
                return
            except Exception as e:
                if attempt < 2:
                    await asyncio.sleep(1.0)
                    continue
                logger.warning("浏览器预热失败: %s", e)

    warmup_task = asyncio.create_task(_warmup())

    tier_task = None

    async def _auto_refresh_tiers():
        import json

        from aistudio_api.infrastructure.account.cookie_parser import (
            probe_account_benefit_tier,
            probe_account_email,
        )

        for a in account_store.list_accounts():
            if not a.email or a.tier == "Free":
                apath = account_store.get_auth_path(a.id)
                if apath and apath.is_file():
                    try:
                        adata = json.loads(apath.read_text(encoding="utf-8"))
                        rcookies = adata.get("cookies") or []
                        cdict = {
                            str(c.get("name") or ""): str(c.get("value") or "")
                            for c in rcookies
                            if isinstance(c, dict) and c.get("name")
                        }
                        if cdict:
                            if not a.email:
                                em = await probe_account_email(
                                    cdict, auth_user=a.auth_user or "0"
                                )
                                if em:
                                    account_store.update_account_email(a.id, em)
                            if a.tier == "Free":
                                t = await probe_account_benefit_tier(
                                    cdict, auth_user=a.auth_user or "0"
                                )
                                if t and t != "Free":
                                    account_store.update_account_tier(a.id, t)
                                    logger.info(
                                        "启动自动校验并升级账号 %s (u/%s) 订阅等级: %s",
                                        a.id,
                                        a.auth_user,
                                        t,
                                    )
                    except Exception:
                        pass

    tier_task = asyncio.create_task(_auto_refresh_tiers())

    async def _account_maintenance():
        from aistudio_api.application.account_health import (
            check_all_accounts,
            keepalive_active_session,
        )

        await asyncio.sleep(30)
        # 禁用名单（AISTUDIO_BLOCKED_EMAILS）中的账号一律停用，避免主号被调度
        try:
            disabled = account_service.disable_blocked_accounts()
            if disabled:
                logger.warning(
                    "已自动停用 %d 个禁用名单中的账号: %s",
                    len(disabled),
                    ", ".join(a.name for a in disabled),
                )
        except Exception as exc:
            logger.error("执行禁用名单检查失败: %s", exc)
        last_health = 0.0
        last_keepalive = time.monotonic()
        while True:
            now = time.monotonic()
            if (
                settings.account_health_interval > 0
                and now - last_health >= settings.account_health_interval
            ):
                last_health = now
                try:
                    summary = await check_all_accounts(account_service, rotator)
                    if summary["expired"]:
                        logger.error(
                            "账号巡检发现 %d 个登录态失效: %s",
                            len(summary["expired"]),
                            ", ".join(str(i) for i in summary["expired"]),
                        )
                    else:
                        logger.info(
                            "账号巡检完成: 有效 %s / 共 %s，未知 %s",
                            summary["valid"],
                            summary["checked"],
                            len(summary["unknown"]),
                        )
                except Exception as e:
                    logger.warning("账号巡检失败: %s", e)
            if (
                settings.session_keepalive_interval > 0
                and now - last_keepalive >= settings.session_keepalive_interval
            ):
                last_keepalive = now
                try:
                    await keepalive_active_session()
                except Exception as e:
                    logger.warning("会话保活任务异常: %s", e)
            await asyncio.sleep(60)

    maintenance_task = asyncio.create_task(_account_maintenance())

    yield
    logger.info("服务正在关闭")
    if warmup_task and not warmup_task.done():
        warmup_task.cancel()
    if tier_task and not tier_task.done():
        tier_task.cancel()
    if not maintenance_task.done():
        maintenance_task.cancel()
    if client:
        try:
            await client.close()
        except Exception as e:
            logger.debug("关闭客户端失败: %s", e)
    runtime_state.client = None
    runtime_state.account_service = None
    runtime_state.rotator = None


app = FastAPI(title="AI Studio API", lifespan=lifespan)
app.include_router(system_public_router)
app.include_router(system_protected_router, dependencies=[Depends(require_web_auth)])
app.include_router(
    accounts_router, prefix="/api", dependencies=[Depends(require_web_auth)]
)
app.include_router(accounts_router, dependencies=[Depends(require_web_auth)])
app.include_router(gemini_router, dependencies=[Depends(require_api_key)])
app.include_router(models_router, dependencies=[Depends(require_api_key)])
app.include_router(openai_router, dependencies=[Depends(require_api_key)])
app.include_router(anthropic_router, dependencies=[Depends(require_api_key)])


def _openai_error_type(status: int) -> str:
    if status == 400:
        return "invalid_request_error"
    if status == 401:
        return "authentication_error"
    if status == 403:
        return "permission_error"
    if status == 404:
        return "not_found_error"
    if status == 429:
        return "rate_limit_error"
    return "api_error"


def _anthropic_error_type(status: int) -> str:
    return _openai_error_type(status)


def _compat_error_response(
    request: Request, status: int, message: str
) -> JSONResponse | None:
    path = request.url.path
    if path.startswith("/v1/messages"):
        return JSONResponse(
            status_code=status,
            content={
                "type": "error",
                "error": {
                    "type": _anthropic_error_type(status),
                    "message": message,
                },
            },
        )
    if path.startswith(("/v1/chat/completions", "/v1/models")):
        return JSONResponse(
            status_code=status,
            content={
                "error": {
                    "message": message,
                    "type": _openai_error_type(status),
                    "param": None,
                    "code": status,
                }
            },
        )
    return None


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    code, msg, status_str = classify_gemini_error_payload(exc)
    compat = _compat_error_response(request, code, msg)
    if compat is not None:
        return compat
    return JSONResponse(
        status_code=code,
        content={
            "error": {
                "code": code,
                "message": msg,
                "status": status_str,
            }
        },
        headers=getattr(exc, "headers", None),
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    code, msg, status_str = 400, str(exc), "INVALID_ARGUMENT"
    compat = _compat_error_response(request, code, msg)
    if compat is not None:
        return compat
    return JSONResponse(
        status_code=code,
        content={
            "error": {
                "code": code,
                "message": msg,
                "status": status_str,
            }
        },
    )


@app.middleware("http")
async def logging_and_dump_middleware(request, call_next):
    """统一请求生命周期日志与请求转储 (DEBUG) 中间件。"""

    start_time = time.perf_counter()
    path = request.url.path

    is_static = path.startswith(("/static", "/assets")) or path in ("/favicon.ico",)
    # 日志查看与健康检查轮询不写入访问日志/转储，避免把真正的业务日志挤掉
    is_silent_poll = path == "/logs" or path.startswith("/logs/") or path == "/health"
    dump_enabled = is_dump_requests_enabled() and not is_static and not is_silent_poll

    req_id = ""
    body_text = None
    client_str = ""
    headers_dict = None
    query_dict = None
    request_url = ""

    if dump_enabled:
        req_id = f"req_{secrets.token_hex(4)}"
        client_str = (
            f"{request.client.host}:{request.client.port}"
            if request.client
            else "unknown"
        )
        headers_dict = {
            k: _mask_dump_value(k, v, _DUMP_SENSITIVE_HEADERS)
            for k, v in dict(request.headers).items()
        }
        query_dict = {
            k: _mask_dump_value(k, v, _DUMP_SENSITIVE_QUERY_KEYS)
            for k, v in dict(request.query_params).items()
        }
        request_url = _masked_dump_url(str(request.url), query_dict)
        try:
            body_bytes = await request.body()
            body_text = body_bytes.decode("utf-8", errors="replace")
        except Exception:
            body_text = "(failed to read request body)"

    try:
        response = await call_next(request)
    except Exception as exc:
        elapsed_ms = (time.perf_counter() - start_time) * 1000
        if dump_enabled:
            dump_request_exchange(
                req_id=req_id,
                method=request.method,
                url=request_url,
                client=client_str,
                headers=headers_dict or {},
                query_params=query_dict or {},
                body_text=body_text,
                status_code=500,
                elapsed_ms=elapsed_ms,
                response_text=f"Internal Server Error (Exception: {exc})",
            )
        else:
            logger.error(
                "HTTP %s %s -> 500 (%.1fms, %s)",
                request.method,
                path,
                elapsed_ms,
                exc,
            )
        raise exc

    elapsed_ms = (time.perf_counter() - start_time) * 1000
    content_type = response.headers.get("content-type", "")
    is_event_stream = "text/event-stream" in content_type

    if dump_enabled:
        resp_text = None
        if is_event_stream:
            resp_text = "(streaming response in progress)"
        else:
            try:
                # 读取非流式响应体并重构 iterator 以便正常返回给客户端
                chunks = [chunk async for chunk in response.body_iterator]
                resp_bytes = b"".join(chunks)
                resp_text = resp_bytes.decode("utf-8", errors="replace")

                async def _replay_iterator():
                    for c in chunks:
                        yield c

                response.body_iterator = _replay_iterator()
            except Exception:
                resp_text = "(failed to read response body)"

        dump_request_exchange(
            req_id=req_id,
            method=request.method,
            url=request_url,
            client=client_str,
            headers=headers_dict or {},
            query_params=query_dict or {},
            body_text=body_text,
            status_code=response.status_code,
            elapsed_ms=elapsed_ms,
            response_headers={
                k: _mask_dump_value(k, v, _DUMP_SENSITIVE_HEADERS)
                for k, v in response.headers.items()
            },
            response_text=resp_text,
            is_stream=is_event_stream,
        )
    elif not is_static and not is_silent_poll:
        logger.info(
            "HTTP %s %s -> %d (%.1fms)",
            request.method,
            path,
            response.status_code,
            elapsed_ms,
        )

    return response


# 挂载前端静态资源与 SPA 路由支持
static_dir = Path(__file__).resolve().parents[1] / "static"
index_html_path = static_dir / "index.html"

if static_dir.is_dir():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    assets_dir = static_dir / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")


@app.get("/auth/check")
async def auth_check():
    """检查认证状态，用于前端判断是否需要登录。"""
    from aistudio_api.infrastructure.auth.web_password import web_auth_enabled

    return {"auth_enabled": web_auth_enabled()}


@app.middleware("http")
async def spa_navigation_middleware(request: Request, call_next):
    """统一处理前端 SPA 单页应用导航：浏览器页面直达/刷新响应 index.html，API 请求直通 JSON。"""
    accept = request.headers.get("accept", "")
    is_html_nav = (
        request.method == "GET"
        and "text/html" in accept
        and "application/json" not in accept
    )
    is_api = request.url.path.startswith(
        (
            "/api",
            "/v1beta",
            "/auth",
            "/assets",
            "/static",
            "/userscripts",
            "/health",
            "/stats",
            "/rotation",
            "/config",
            "/api-keys",
        )
    )
    is_accounts_spa = request.url.path in ("/accounts", "/accounts/")
    if is_html_nav and (is_accounts_spa or not is_api) and index_html_path.is_file():
        response = FileResponse(index_html_path)
        response.headers["Vary"] = "Accept"
        response.headers["Cache-Control"] = (
            "no-store, no-cache, must-revalidate, max-age=0"
        )
        return response

    response = await call_next(request)

    if request.url.path.startswith(("/accounts", "/api/accounts")) or is_html_nav:
        existing_vary = response.headers.get("Vary")
        if existing_vary:
            if "Accept" not in [v.strip() for v in existing_vary.split(",")]:
                response.headers["Vary"] = f"{existing_vary}, Accept"
        else:
            response.headers["Vary"] = "Accept"
        response.headers["Cache-Control"] = (
            "no-store, no-cache, must-revalidate, max-age=0"
        )

    return response


@app.get("/")
async def root_index():
    """根路径兜底响应。"""
    if index_html_path.is_file():
        response = FileResponse(index_html_path)
        response.headers["Vary"] = "Accept"
        response.headers["Cache-Control"] = (
            "no-store, no-cache, must-revalidate, max-age=0"
        )
        return response
    return JSONResponse({"message": "AI Studio API Web UI"})


def main():
    from aistudio_api.config import settings

    parser = argparse.ArgumentParser(description="AI Studio API Server")
    parser.add_argument("--port", type=int, default=settings.port)
    parser.add_argument("--browser-port", type=int, default=settings.browser_port)
    args = parser.parse_args()

    runtime_state.browser_port = args.browser_port

    import uvicorn

    logger.info("正在启动服务，监听端口 %s", args.port)
    uvicorn.run(app, host="0.0.0.0", port=args.port, log_level="info")
