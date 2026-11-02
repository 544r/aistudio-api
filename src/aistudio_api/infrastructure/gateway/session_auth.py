"""Authentication, cookies, and identity verification helpers for AI Studio sessions."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlsplit

from aistudio_api.domain.errors import SessionExpiredError
from aistudio_api.infrastructure.account.cookie_parser import (
    DEFAULT_API_KEY,
    DEFAULT_EXT_BIN,
    DEFAULT_USER_AGENT,
    ROLLING_COOKIE_NAMES,
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
                tier = str(meta.get("tier") or "Free").strip().capitalize()
                if tier in ("Free", "Pro", "Ultra", "Plus"):
                    return tier
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
# 注意 SIDCC / *PSIDCC 不在此列——它们属于 Google 全域会话链路，丢掉就等于把
# 轮换起点交给一次未知的重新协商，见 cookie_parser.ROLLING_COOKIE_NAMES。
_INJECTION_SKIPPED_COOKIE_NAMES = frozenset({"OSID", "__Secure-OSID"})


async def load_cookies_to_page(
    page: CDPPage, auth_file: str | None, *, preserve_rolling: bool = False
) -> bool:
    """Inject cookies from auth_file into the target page.

    `preserve_rolling=True` 用于**同一账号**的热刷新（油猴推送覆盖、会话内重载）：
    既不清空浏览器 Cookie 罐，也不覆盖罐里已有的滚动代次（`*PSIDTS` /
    `*PSIDRTS` / `SIDCC` / `*PSIDCC`）。磁盘快照最多可能落后一个轮换周期，把旧
    代次灌回正在工作的会话，等于主动向 Google 提交上一代凭据——上游判定重放后
    返回 401，账号就被标记成“登录态失效”。长寿命凭据（SID / PSID / APISID 系）
    仍照常覆盖，推送刷新登录根的目的不受影响。

    换账号时必须走默认的 `preserve_rolling=False`：那是要整个罐子换掉，
    否则会残留上一个账号的代次。
    """
    if not auth_file or not Path(auth_file).is_file():
        return False
    page.account_auth_file = auth_file
    try:
        live_rolling: dict[str, str] = {}
        if preserve_rolling:
            with suppress(Exception):
                live_rolling = {
                    str(item.get("name") or ""): str(item.get("value") or "")
                    for item in await page.get_cookies()
                    if isinstance(item, dict)
                    and str(item.get("name") or "") in ROLLING_COOKIE_NAMES
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

        cleaned_cookies = [
            c
            for c in cookies
            if isinstance(c, dict)
            and str(c.get("name") or "") not in _INJECTION_SKIPPED_COOKIE_NAMES
            and str(c.get("name") or "") not in live_rolling
        ]
        if not cleaned_cookies:
            # 磁盘里只剩被罐内实时值保护的滚动代次：注入等价于空操作，会话仍然可用
            return bool(live_rolling)
        await page.set_cookies(cleaned_cookies)
        # set_cookies 会用本次注入列表重建 _cached_cookies，把被保护的实时
        # 代次补回去，避免签名计算读到残缺的 Cookie 视图
        cached = getattr(page, "_cached_cookies", None)
        if isinstance(cached, dict):
            cached.update(live_rolling)
        log.info(
            "已向页面载入 %d 个 Cookie (auth_file=%s, 保留实时滚动代次 %d 项: %s)",
            len(cleaned_cookies),
            auth_file,
            len(live_rolling),
            ", ".join(sorted(live_rolling)) or "-",
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
        previous_names: set[str] = set()
        if auth_path.exists():
            with suppress(Exception):
                existing = json.loads(auth_path.read_text(encoding="utf-8"))
                origins = existing.get("origins", [])
                previous = existing.get("cookies")
                if isinstance(previous, list):
                    previous_names = {
                        str(item.get("name") or "")
                        for item in previous
                        if isinstance(item, dict)
                    }
        # 浏览器落盘的滚动代次由服务端（浏览器会话本身）推进过：打上标记，
        # 推送合并时据此保护这份代次不被另一台浏览器的旧代次顶回。
        #
        # 判据是「滚动代次在浏览器里确实出现了，而上一份 auth.json 里没有」。
        # 每次导航都无条件置位会让标记恒真：推送带来的新代次永远进不来，服务端
        # 守着旧代次直到被判过期。宁可少保护（推送接管）也不要把它变成单向锁。
        rolling_names = ROLLING_COOKIE_NAMES
        browser_rolling = {
            str(item.get("name") or "")
            for item in current_cookies
            if isinstance(item, dict)
            and str(item.get("name") or "") in rolling_names
            and item.get("value")
        }
        state: dict[str, object] = {"cookies": current_cookies, "origins": origins}
        if browser_rolling - previous_names:
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
                # 登录页重注入不清空浏览器罐：罐里的滚动代次可能比 auth.json 快照
                # 新，清罐重灌等于把旧代次再交一遍，Google 会直接判会话异常。
                if not await load_cookies_to_page(
                    page, auth_file, preserve_rolling=True
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
