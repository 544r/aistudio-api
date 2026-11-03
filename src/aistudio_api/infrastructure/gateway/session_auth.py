"""Authentication, cookies, and identity verification helpers for AI Studio sessions."""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlsplit

from aistudio_api.domain.errors import SessionExpiredError
from aistudio_api.infrastructure.account.cookie_parser import (
    ACCOUNTS_ROOT_COOKIE_NAMES,
    DEFAULT_API_KEY,
    DEFAULT_EXT_BIN,
    DEFAULT_USER_AGENT,
    GLOBAL_GENERATION_COOKIE_NAMES,
    SERVICE_SESSION_COOKIE_NAMES,
    normalize_benefit_tier,
    parse_benefit_tier_response,
)
from aistudio_api.infrastructure.browser.cdp_client import CDPPage
from aistudio_api.infrastructure.browser.scripts import (
    CHECK_IDENTITY_JS,
    EXTRACT_EMAIL_JS,
)
from aistudio_api.infrastructure.utils.common import atomic_write_json, mask_email
from aistudio_api.infrastructure.utils.logger import get_logger

log = get_logger("session.auth")

AI_STUDIO_URL = (
    "https://aistudio.google.com/prompts/new_chat?model=gemini-3.7-flash&temporary=true"
)
AI_STUDIO_URL_FALLBACK = (
    "https://aistudio.google.com/app/prompts/new_chat?temporary=true"
)
GOOGLE_LOGIN_BOOTSTRAP_URL = (
    "https://accounts.google.com/ServiceLogin?continue=https://aistudio.google.com"
)


def is_login_page_url(url: str | None) -> bool:
    """True if URL matches Google account signin / login paths."""
    if not url:
        return False
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        path = (parsed.path or "").lower()
        query = (parsed.query or "").lower()
    except Exception:
        return False
    if host == "accounts.google.com" or host.endswith(".accounts.google.com"):
        return True
    return (
        "signin" in path
        or "servicelogin" in path
        or "accountchooser" in path
        or "signin" in query
        or "servicelogin" in query
    )


def is_aistudio_page_url(url: str | None) -> bool:
    """True if URL points to aistudio.google.com and is not a Google login page."""
    if not url or is_login_page_url(url):
        return False
    try:
        host = (urlsplit(url).hostname or "").lower()
        return host == "aistudio.google.com" or host.endswith(".aistudio.google.com")
    except Exception:
        return False


def derive_profile_dir(auth_file: str | None) -> str | None:
    """Derive user data profile directory corresponding to an auth file."""
    if not auth_file:
        return None
    return str(Path(auth_file).resolve().parent / "profile")


def get_auth_user_from_meta(auth_file: str | None) -> str:
    """Read auth_user index from meta.json adjacent to auth_file."""
    if auth_file:
        try:
            meta_path = Path(auth_file).parent / "meta.json"
            if meta_path.exists():
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                return str(meta.get("auth_user") or "0")
        except Exception:
            pass
    return "0"


def get_tier_from_meta(auth_file: str | None) -> str:
    """Read benefit tier from meta.json adjacent to auth_file."""
    if auth_file:
        try:
            meta_path = Path(auth_file).parent / "meta.json"
            if meta_path.exists():
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                return normalize_benefit_tier(str(meta.get("tier") or "Free"))
        except Exception:
            pass
    return "Free"


def get_aistudio_urls(
    auth_user: str = "0", model: str = "gemini-3.7-flash"
) -> list[str]:
    """Build AI Studio navigation URLs for target auth_user."""
    if auth_user and auth_user != "0":
        return [
            f"https://aistudio.google.com/u/{auth_user}/prompts/new_chat?model={model}&temporary=true",
            f"https://aistudio.google.com/u/{auth_user}/app/prompts/new_chat?temporary=true",
        ]
    return [AI_STUDIO_URL, AI_STUDIO_URL_FALLBACK]


# 注入时一律跳过的键：AI Studio 的站点级会话 ID 与浏览器上下文绑定，跨设备复用
# 旧值反而容易被上游判成会话冲突，交给浏览器自己协商一个新的。
# SIDCC 系全局代次同样永远跳过：Google 对任何响应都会重发它们，从磁盘回放旧值
# 属于重放，上游直接 401。
_INJECTION_SKIPPED_COOKIE_NAMES = (
    frozenset({"OSID", "__Secure-OSID"}) | GLOBAL_GENERATION_COOKIE_NAMES
)


async def load_cookies_to_page(
    page: CDPPage,
    auth_file: str | None,
    *,
    clear_first: bool = False,
    include_rolling: bool = False,
) -> bool:
    """Inject cookies from auth_file into the target page.

    Cookie 所有权分两层：

    - SIDCC 系全局代次：权威来源永远是浏览器罐（Google 任何响应都重发），
      注入永远跳过，磁盘旧值灌回去就是重放 → 401。
    - PSIDTS/PSIDRTS/`__Olefcr-*` 服务级会话票：aistudio.google.com 凭它认
      登录态，缺失即被弹回登录页且 Google 不会补发。默认从 auth.json 注入，
      但罐里已有实时代次时罐优先（热刷新不回滚）；`include_rolling=True`
      （RotateCookies 抢救 / 推送接管）强制用磁盘最新值覆盖。
    - 长寿命凭据（SID / PSID / APISID 系）照常覆盖。

    - 默认：不清罐。适用于同账号热刷新、冷启动加载本账号 profile、登录页重注入。
    - `clear_first=True`（换账号）：先清空罐与缓存再注入，防止上一个账号的
      凭据残留。
    - `include_rolling=True`（RotateCookies 抢救 / 推送接管）：auth.json 刚被
      RotateCookies 响应或推送到最新一代，此刻磁盘才是权威，强制覆盖服务级会话票。
    """
    if not auth_file or not Path(auth_file).is_file():
        return False
    page.account_auth_file = auth_file
    try:
        live_service_tokens: dict[str, str] = {}
        if not clear_first:
            with suppress(Exception):
                live_service_tokens = {
                    str(item.get("name") or ""): str(item.get("value") or "")
                    for item in await page.get_cookies()
                    if isinstance(item, dict)
                    and str(item.get("name") or "") in SERVICE_SESSION_COOKIE_NAMES
                    and item.get("value")
                }
        else:
            with suppress(Exception):
                await page.cdp.send("Network.clearBrowserCookies")
                await page.cdp.send("Network.clearBrowserCache")

        data = json.loads(Path(auth_file).read_text(encoding="utf-8"))
        cookies = data.get("cookies") or []
        if not cookies:
            return False

        skipped = _INJECTION_SKIPPED_COOKIE_NAMES
        if not include_rolling:
            # 罐里已有的服务级会话票以罐为准（热刷新不回滚代次）
            skipped = skipped | set(live_service_tokens)
        cleaned_cookies = [
            c
            for c in cookies
            if isinstance(c, dict) and str(c.get("name") or "") not in skipped
        ]
        if not cleaned_cookies:
            # 磁盘里只剩被跳过的键：注入等价于空操作，
            # 会话是否可用取决于罐里是否已有实时凭据
            return bool(live_service_tokens)
        await page.set_cookies(cleaned_cookies)
        # set_cookies 会用本次注入列表重建 _cached_cookies；不清罐的场景罐里
        # 还留着本账号的实时代次，补回缓存避免签名/探活读到残缺视图。
        # include_rolling 时注入列表已带最新值，绝不能再用注入前的旧值覆盖；
        # 清罐场景 live_service_tokens 必为空，不会把上一个账号的代次带进缓存。
        cached = getattr(page, "_cached_cookies", None)
        if isinstance(cached, dict) and not include_rolling:
            cached.update(live_service_tokens)
        log.info(
            "已向页面载入 %d 个 Cookie (auth_file=%s, 清罐=%s, 含服务会话票=%s, "
            "保留罐内实时代次 %d 项: %s)",
            len(cleaned_cookies),
            auth_file,
            clear_first,
            include_rolling,
            len(live_service_tokens),
            ", ".join(sorted(live_service_tokens)) or "-",
        )
        return True
    except Exception as e:
        log.warning("向页面注入 Cookie 失败: %s", e)
        return False


async def extract_account_email(
    page: CDPPage | None, auth_user: str = "0"
) -> str | None:
    """Extract authenticated Google email from page DOM (bound to u/{auth_user})."""
    if page is None or page.is_closed():
        return None
    try:
        raw_email = await page.evaluate(
            EXTRACT_EMAIL_JS, args={"auth_user": str(auth_user or "0")}
        )
        if raw_email and isinstance(raw_email, str) and "@" in raw_email:
            return raw_email.strip()
    except Exception as e:
        log.debug("从页面提取账号邮箱失败: %s", e)
    return None


async def extract_account_tier(
    page: CDPPage | None, auth_user: str = "0"
) -> str | None:
    """Extract authenticated Google AI benefit tier via in-page GetAiStudioBenefitTier XHR."""
    if page is None or page.is_closed():
        return None
    from aistudio_api.infrastructure.account.cookie_parser import (
        calculate_sapisid_hash,
    )

    auth_header = ""
    with suppress(Exception):
        cookie_dict = getattr(page, "_cached_cookies", None)
        if not cookie_dict:
            cookie_list = await page.get_cookies()
            cookie_dict = {
                str(c.get("name") or ""): str(c.get("value") or "")
                for c in cookie_list
                if isinstance(c, dict) and c.get("name")
            }
        if cookie_dict:
            auth_header = calculate_sapisid_hash(cookie_dict)

    script = """
    (args) => {
        return new Promise((resolve) => {
            const xhr = new XMLHttpRequest();
            xhr.open('POST', 'https://alkalimakersuite-pa.clients6.google.com/$rpc/google.internal.alkali.applications.makersuite.v1.MakerSuiteService/GetAiStudioBenefitTier');
            xhr.setRequestHeader('Content-Type', 'application/json+protobuf');
            xhr.setRequestHeader('X-User-Agent', args.userAgent);
            xhr.setRequestHeader('X-Goog-Api-Key', args.apiKey);
            xhr.setRequestHeader('X-Goog-AuthUser', String(args.authUser || '0'));
            xhr.setRequestHeader('X-Goog-Ext-519733851-bin', args.extBin);
            if (args.authHeader) {
                xhr.setRequestHeader('Authorization', args.authHeader);
            }
            xhr.withCredentials = true;
            xhr.timeout = 8000;
            xhr.onload = () => {
                if (xhr.status === 200) {
                    resolve({ok: true, text: xhr.responseText || '[]'});
                } else {
                    resolve({ok: false, status: xhr.status});
                }
            };
            xhr.onerror = () => resolve({ok: false});
            xhr.ontimeout = () => resolve({ok: false});
            xhr.send('[]');
        });
    }
    """
    try:
        res = await page.evaluate(
            script,
            {
                "authUser": str(auth_user or "0"),
                "apiKey": DEFAULT_API_KEY,
                "userAgent": DEFAULT_USER_AGENT,
                "extBin": DEFAULT_EXT_BIN,
                "authHeader": auth_header,
            },
            timeout_s=10.0,
        )
        if isinstance(res, dict) and res.get("ok"):
            return parse_benefit_tier_response(res.get("text"))
    except Exception as e:
        log.debug("从页面提取账号订阅等级失败: %s", e)
    return None


async def verify_account_identity(page: CDPPage, auth_file: str | None) -> bool:
    """Verify currently loaded account matches metadata in auth_file."""
    if not auth_file:
        return True
    meta_path = Path(auth_file).parent / "meta.json"
    if not meta_path.exists():
        return True
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return True

    account_id = meta.get("id", "unknown")
    auth_user = str(meta.get("auth_user") or "0")

    curr_url = str(getattr(page, "url", "") or "")
    is_on_correct_user = (
        f"/u/{auth_user}/" in curr_url
        if auth_user != "0"
        else ("/u/0/" in curr_url or "/u/" not in curr_url)
    )

    expected_email = (meta.get("email") or "").strip().lower()
    discovered_email: str | None = None

    if not expected_email and is_on_correct_user:
        discovered_email = await extract_account_email(page, auth_user=auth_user)
        if discovered_email:
            log.info(
                "已自动识别账号 %s (u/%s) 登录邮箱: %s",
                account_id,
                auth_user,
                mask_email(discovered_email),
            )
            meta["email"] = discovered_email
            expected_email = discovered_email.strip().lower()
            with suppress(Exception):
                atomic_write_json(meta_path, meta)
            with suppress(Exception):
                from aistudio_api.infrastructure.account.account_store import (
                    AccountStore,
                )

                if account_id and account_id != "unknown":
                    AccountStore().update_account_email(account_id, discovered_email)

    if is_on_correct_user and (not meta.get("tier") or meta.get("tier") == "Free"):
        discovered_tier = await extract_account_tier(page, auth_user=auth_user)
        if not discovered_tier or discovered_tier == "Free":
            try:
                auth_data = json.loads(Path(auth_file).read_text(encoding="utf-8"))
                cookie_dict = {
                    str(c.get("name") or ""): str(c.get("value") or "")
                    for c in auth_data.get("cookies", [])
                    if isinstance(c, dict) and c.get("name")
                }
                if cookie_dict:
                    from aistudio_api.infrastructure.account.cookie_parser import (
                        probe_account_benefit_tier,
                    )

                    http_tier = await probe_account_benefit_tier(
                        cookie_dict, auth_user=auth_user
                    )
                    if http_tier and http_tier != "Free":
                        discovered_tier = http_tier
            except Exception:
                pass

        if discovered_tier:
            meta["tier"] = discovered_tier
            with suppress(Exception):
                atomic_write_json(meta_path, meta)
            with suppress(Exception):
                from aistudio_api.infrastructure.account.account_store import (
                    AccountStore,
                )

                if account_id and account_id != "unknown":
                    AccountStore().update_account_tier(account_id, discovered_tier)

    if not expected_email:
        return is_on_correct_user
    if (
        discovered_email
        and discovered_email.strip().lower() == expected_email
        and is_on_correct_user
    ):
        return True

    if (
        discovered_email
        and discovered_email.strip().lower() != expected_email
        and not is_on_correct_user
    ):
        log.warning(
            "[account-guard] 页面检测到不同账号 %s (期望 %s, acc_id=%s, u/%s, url=%s)，跳过本次 Cookie 回写以防覆盖",
            discovered_email,
            expected_email,
            account_id,
            auth_user,
            curr_url,
        )
        return False

    is_verified = False
    with suppress(Exception):
        is_verified = bool(
            await page.evaluate(
                CHECK_IDENTITY_JS,
                {"email": expected_email, "authUser": auth_user},
            )
        )

    if (
        not is_verified
        and auth_user != "0"
        and f"/u/{auth_user}/" in curr_url
        and (not discovered_email or discovered_email.strip().lower() == expected_email)
    ):
        is_verified = True

    return is_verified


def _cookie_is_unexpired(cookie: dict[str, object], now: float) -> bool:
    """expires 为 -1/0 表示会话 Cookie；正数必须是未来时间。"""
    expires = cookie.get("expires")
    if not isinstance(expires, (int, float)) or isinstance(expires, bool):
        return True
    return float(expires) <= 0 or float(expires) > now


async def save_browser_cookies(
    page: CDPPage,
    auth_file: str | None,
    cookies: list[dict[str, object]] | None = None,
    *,
    quiet: bool = False,
) -> bool:
    """Save browser cookies back to auth_file JSON atomically."""
    if not auth_file:
        return False
    try:
        current_cookies = cookies
        if current_cookies is None:
            if page.is_closed():
                return False
            current_cookies = await page.get_cookies()
        if not current_cookies:
            return False
        auth_path = Path(auth_file)
        origins = []
        previous_cookies: list[dict[str, object]] = []
        if auth_path.exists():
            with suppress(Exception):
                existing = json.loads(auth_path.read_text(encoding="utf-8"))
                origins = existing.get("origins", [])
                previous = existing.get("cookies")
                if isinstance(previous, list):
                    previous_cookies = [
                        item for item in previous if isinstance(item, dict)
                    ]
        current_cookies = [item for item in current_cookies if isinstance(item, dict)]
        current_names = {str(item.get("name") or "") for item in current_cookies}
        # AI Studio 页面的浏览器罐看不到 accounts.google.com 的 host-only 根凭据
        # （LSID / __Host-*PLSID，RotateCookies 抢救的原料），也拿不到服务级会话票
        # （PSIDTS/PSIDRTS/Olefcr——会话被弹回登录页时罐里就是没有）。原样回写会让
        # 推送带进来的这些凭据在第一次自动保存后消失。这里从上一份 auth.json 里
        # 补回来；已经过期或浏览器显式删除（空值/不在罐里）的不补。
        now = time.time()
        preserve_names = ACCOUNTS_ROOT_COOKIE_NAMES | SERVICE_SESSION_COOKIE_NAMES
        preserved_roots = [
            dict(item)
            for item in previous_cookies
            if str(item.get("name") or "") in preserve_names
            and str(item.get("name") or "") not in current_names
            and item.get("value")
            and _cookie_is_unexpired(item, now)
        ]
        if preserved_roots:
            current_cookies = [*current_cookies, *preserved_roots]
            log.debug(
                "浏览器保存保留 %d 个镜像凭据 (根凭据/服务会话票): %s",
                len(preserved_roots),
                ", ".join(str(item.get("name") or "") for item in preserved_roots),
            )
        # 浏览器落盘的滚动代次由服务端（浏览器会话本身）推进过：打上标记，
        # 供轮换流水/排查判断这份 auth.json 的代次来源。
        #
        # 判据是服务级会话票的**值**发生了变化（或出现了新名字），而不只是
        # 名字首次出现——PSIDTS/PSIDRTS 每次轮换都是同名换值，按名字判断会
        # 让标记永远不更新，历史上据此保护代次时会把服务端已推进的新值误判
        # 成“从未推进”。SIDCC 系 Google 对任何响应（包括未登录请求）都会
        # 重发，不构成服务会话推进，不参与判断。
        previous_service_values = {
            str(item.get("name") or ""): str(item.get("value") or "")
            for item in previous_cookies
            if str(item.get("name") or "") in SERVICE_SESSION_COOKIE_NAMES
            and item.get("value")
        }
        current_service_values = {
            str(item.get("name") or ""): str(item.get("value") or "")
            for item in current_cookies
            if isinstance(item, dict)
            and str(item.get("name") or "") in SERVICE_SESSION_COOKIE_NAMES
            and item.get("value")
        }
        state: dict[str, object] = {"cookies": current_cookies, "origins": origins}
        if any(
            value != previous_service_values.get(name)
            for name, value in current_service_values.items()
        ):
            state["server_rotated"] = True
        atomic_write_json(auth_path, state)
        if quiet:
            log.debug("已保存 %d 个 Cookie 至 %s", len(current_cookies), auth_file)
        else:
            log.info("已保存 %d 个 Cookie 至 %s", len(current_cookies), auth_file)
        return True
    except Exception as e:
        log.debug("保存 Cookie 失败: %s", e)
        return False


async def bootstrap_google_session(page: CDPPage) -> None:
    """Navigate through Google login bootstrap surface."""
    await page.goto(
        GOOGLE_LOGIN_BOOTSTRAP_URL, wait_until="domcontentloaded", timeout_s=30.0
    )
    await page.wait_for_timeout(3000)
    for url in (AI_STUDIO_URL, AI_STUDIO_URL_FALLBACK):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout_s=30.0)
            await page.wait_for_timeout(1500)
            if "accounts.google.com" not in (page.url or ""):
                return
        except Exception:
            continue
    raise RuntimeError(f"bootstrap stayed on login flow: url={page.url}")


async def _goto_url_with_abort_tolerance(
    page: CDPPage, url: str, auth_user: str = "0"
) -> str:
    """Navigate page to target AI Studio URL, tolerating net::ERR_ABORTED if already on target."""
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout_s=20.0)
    except RuntimeError as nav_exc:
        if "net::ERR_ABORTED" in str(nav_exc):
            curr = page.url or ""
            with suppress(Exception):
                eval_url = await page.evaluate(
                    "() => window.location.href", timeout_s=2.0
                )
                if eval_url:
                    curr = str(eval_url)
            expected_match = (
                f"/u/{auth_user}/"
                if auth_user and auth_user != "0"
                else "aistudio.google.com"
            )
            curr_parsed = urlsplit(curr)
            if is_aistudio_page_url(curr) and (
                expected_match in curr_parsed.path
                or expected_match == "aistudio.google.com"
            ):
                log.debug("页面导航被终止但已处于 AI Studio 域: %s", curr)
            else:
                raise
        else:
            raise

    current_url = page.url or ""
    with suppress(Exception):
        eval_url = await page.evaluate("() => window.location.href", timeout_s=2.0)
        if eval_url:
            current_url = str(eval_url)
    return current_url


async def navigate_to_aistudio(
    page: CDPPage,
    auth_user: str = "0",
    auth_file: str | None = None,
    verify_identity_cb: Callable[[CDPPage], Awaitable[bool]] | None = None,
    save_cookies_cb: Callable[..., Awaitable[None]] | None = None,
) -> None:
    """Navigate page to AI Studio with region and session-expiration checks."""
    last_exc = None
    target_urls = get_aistudio_urls(auth_user=auth_user)
    for url in target_urls:
        try:
            current_url = await _goto_url_with_abort_tolerance(
                page, url, auth_user=auth_user
            )

            if "available-regions" in current_url:
                raise RuntimeError(
                    f"Google AI Studio 地区限制 (IP 漏了/不支持): {current_url}"
                )
            if is_login_page_url(current_url):
                log.warning(
                    "访问 AI Studio 后检测到重定向至登录页: %s，尝试重新注入 Cookie...",
                    current_url,
                )
                # 登录页重注入不清空浏览器罐：罐里的长寿命凭据和 SIDCC 仍由
                # 浏览器持有。但服务会话票要强制用 auth.json 的最新值——页面
                # 被弹到登录页本身就是罐里那代票认证失败的证据，继续“罐优先”
                # 只会把死票再交一遍。
                if not await load_cookies_to_page(
                    page, auth_file, include_rolling=True
                ):
                    raise SessionExpiredError(
                        f"Cookie 认证失败，已被重定向到 Google 登录页。 (url={current_url})"
                    )
                current_url = await _goto_url_with_abort_tolerance(
                    page, url, auth_user=auth_user
                )
                if "available-regions" in current_url:
                    raise RuntimeError(
                        f"Google AI Studio 地区限制 (IP 漏了/不支持): {current_url}"
                    )
                if is_login_page_url(current_url) or not is_aistudio_page_url(
                    current_url
                ):
                    raise SessionExpiredError(
                        f"Cookie 认证失败，重新注入并导航后仍处于登录页。 (url={current_url})"
                    )

            for _ in range(15):
                with suppress(Exception):
                    if await page.evaluate("() => !!window.default_MakerSuite"):
                        break
                await page.wait_for_timeout(500)

            curr_after_wait = getattr(page, "url", "") or ""
            if is_login_page_url(curr_after_wait):
                raise SessionExpiredError(
                    f"Cookie 认证失败，等待页面就绪期间重定向到 Google 登录页。 (url={curr_after_wait})"
                )

            if verify_identity_cb is not None:
                is_verified = await verify_identity_cb(page)
            else:
                is_verified = await verify_account_identity(page, auth_file)

            if is_verified:
                if save_cookies_cb is not None:
                    await save_cookies_cb(auth_file=auth_file)
                else:
                    await save_browser_cookies(page, auth_file)
            return
        except Exception as exc:
            if (
                isinstance(exc, SessionExpiredError)
                or "地区限制" in str(exc)
                or "Cookie 认证失败" in str(exc)
            ):
                raise exc
            log.debug("访问 %s 失败: %s", url, exc)
            last_exc = exc

    if last_exc is not None:
        raise last_exc
