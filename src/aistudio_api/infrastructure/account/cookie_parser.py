"""解析浏览器 cookie（支持 JSON 数组、Netscape 格式与 Header KV），并提供 Google 多账号探活。"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import time
import uuid
from collections.abc import Iterable
from email.utils import parsedate_to_datetime

from aistudio_api.infrastructure.utils.logger import get_logger

logger = get_logger("cookie_parser")

DEFAULT_API_KEY = "AIzaSyDdP816MREB3SkjZO04QXbjsigfcI0GWOs"
DEFAULT_EXT_BIN = "CAESAUwwATgEQABQBGICSlBwAHgBkAEAmAEB"
DEFAULT_ORIGIN = "https://aistudio.google.com"
DEFAULT_USER_AGENT = "grpc-web-javascript/0.1"


BENEFIT_TIER_TO_NAME: dict[int, str] = {
    0: "Free",
    1: "Pro",
    2: "Ultra",
    3: "Plus",
}

BENEFIT_TIER_TO_HEADER: dict[str, str] = {
    "Pro": "TIER1",
    "Ultra": "TIER2",
    "Plus": "TIER0",
}


def parse_benefit_tier_response(raw_data: object) -> str:
    """解析 GetAiStudioBenefitTier 响应数组为标准权益名称 (Free / Pro / Ultra / Plus)。"""
    if isinstance(raw_data, str):
        text = raw_data.strip().removeprefix(")]}'").strip()
        if text.startswith("[,"):
            text = "[null," + text[2:]
        try:
            raw_data = json.loads(text)
        except Exception:
            return "Free"
    if not isinstance(raw_data, list) or not raw_data or raw_data[0] is None:
        return "Free"
    val = raw_data[0]
    if isinstance(val, bool):
        return "Free"
    if isinstance(val, (int, float)):
        return BENEFIT_TIER_TO_NAME.get(int(val), "Free")
    return "Free"


def get_tier_header_value(tier: str | None) -> str | None:
    """根据账号权益等级返回对应的 X-AIStudio-G1-Tier 请求头取值。"""
    if not tier:
        return None
    clean = tier.strip().capitalize()
    return BENEFIT_TIER_TO_HEADER.get(clean)


# Cookies that need httpOnly=False so JS can read them for SAPISIDHASH.
_AUTH_COOKIE_NAMES = {
    "SID",
    "APISID",
    "SAPISID",
    "__Secure-1PAPISID",
    "__Secure-3PAPISID",
}

_DOMAIN_OVERRIDES: dict[str, list[str]] = {
    "OSID": ["aistudio.google.com"],
    "__Secure-OSID": ["aistudio.google.com"],
    "__Secure-BUCKET": ["aistudio.google.com"],
    "OTZ": ["accounts.google.com"],
    "__Host-GAPS": ["accounts.google.com"],
    "__Host-1PLSID": ["accounts.google.com"],
    "__Host-3PLSID": ["accounts.google.com"],
    "LSID": ["accounts.google.com"],
    "SMSV": ["accounts.google.com"],
    "LSOLH": ["accounts.google.com"],
    "ACCOUNT_CHOOSER": ["accounts.google.com"],
    "__Secure-1PSIDTS": [".google.com", "aistudio.google.com", "accounts.google.com"],
    "__Secure-3PSIDTS": [".google.com", "aistudio.google.com", "accounts.google.com"],
}

# Google 的“滚动代次”凭据：任意一次响应都可能用 Set-Cookie 把它们推进到新一代，
# 旧代次一旦被服务端记录为已替换，再提交就得到 401（表现为“Cookie 突然过期”）。
# 因此这些键只允许向前更新——来自本服务浏览器实测或探活响应的 Set-Cookie——
# 不允许被磁盘快照或另一台浏览器的推送顶回上一代。
ROLLING_COOKIE_NAMES: frozenset[str] = frozenset(
    {
        "__Secure-1PSIDTS",
        "__Secure-3PSIDTS",
        "__Secure-1PSIDRTS",
        "__Secure-3PSIDRTS",
        "SIDCC",
        "__Secure-1PSIDCC",
        "__Secure-3PSIDCC",
    }
)

# accounts.google.com 的 host-only 会话根凭据。它们只在 accounts.google.com
# 域名下可见，AI Studio 页面里的浏览器罐读不到；而 RotateCookies 抢救必须用到
# 它们。浏览器回写 auth.json 时要显式保留，否则推送带进来的根凭据会在第一次
# 自动保存后消失，401 抢救永远缺少原料。
ACCOUNTS_ROOT_COOKIE_NAMES: frozenset[str] = frozenset(
    {"LSID", "__Host-1PLSID", "__Host-3PLSID", "__Host-GAPS"}
)

# 外部导入时可安全丢弃的遥测及旧环境绑定的临时 Cookie。
# 过滤这些 Cookie 既能让网页在当前 IP/环境重新自然下发全新凭据，
# 又能精简内存与 Cookie 存储体积（尤其对 Android 低内存环境极为友好）。
# 注意：SIDCC / *PSIDCC / OSID 属于会话链路，不再丢弃——丢掉了就只能靠浏览器
# 重新协商，而重新协商出来的新代次会让磁盘上那份旧代次立刻作废。
DISCARDABLE_IMPORT_COOKIE_NAMES: frozenset[str] = frozenset(
    {
        "_ga",
        "_gid",
        "_gat",
        "_gcl_au",
        "1P_JAR",
        "DV",
        "AEC",
        "NID",
        "OSID",
        "__Secure-OSID",
    }
)


def parse_raw_cookies(raw: object) -> dict[str, str]:
    """万能 Cookie 解析器，支持：
    1. JSON 数组（EditThisCookie / Cookie-Editor 导出：`[{"name": "...", "value": "..."}, ...]`）
    2. JSON 对象（`{"cookies": [...]}` 或 `{"cookies": {"k": "v"}}`）
    3. Netscape / curl 格式（7 列 Tab 分隔）
    4. HTTP 请求头格式（`Cookie: k1=v1; k2=v2` 或带换行的多行键值对）
    """
    parsed: dict[str, str] = {}
    if not raw:
        return parsed

    text = raw.strip() if isinstance(raw, str) else json.dumps(raw)

    if re.match(r"^cookie:\s*", text, re.IGNORECASE):
        text = re.sub(r"^cookie:\s*", "", text, flags=re.IGNORECASE)

    # 1. 尝试 JSON 格式
    if text.startswith(("{", "[")):
        try:
            obj = json.loads(text)
            if isinstance(obj, list):
                for item in obj:
                    if (
                        isinstance(item, dict)
                        and item.get("name")
                        and item.get("value") is not None
                    ):
                        k = str(item["name"]).strip()
                        v = str(item["value"]).strip()
                        if k and v:
                            parsed[k] = v
                if parsed:
                    return parsed
            elif isinstance(obj, dict):
                if isinstance(obj.get("cookie"), str):
                    text = obj["cookie"]
                elif isinstance(obj.get("cookies"), str):
                    text = obj["cookies"]
                elif isinstance(obj.get("cookies"), list):
                    for item in obj["cookies"]:
                        if (
                            isinstance(item, dict)
                            and item.get("name")
                            and item.get("value") is not None
                        ):
                            k = str(item["name"]).strip()
                            v = str(item["value"]).strip()
                            if k and v:
                                parsed[k] = v
                    if parsed:
                        return parsed
                elif isinstance(obj.get("cookies"), dict):
                    for k, v in obj["cookies"].items():
                        if k and v is not None:
                            parsed[str(k).strip()] = str(v).strip()
                    return parsed
        except Exception:
            pass

    # 2. 按行与分号拆分解析
    lines = re.split(r"[\r\n;]+", text)
    ignored_keys = {
        "domain",
        "path",
        "expires",
        "samesite",
        "secure",
        "httponly",
        "priority",
        "hostonly",
    }

    for line in lines:
        trimmed = line.strip()
        if not trimmed or trimmed.startswith("#"):
            continue

        # Netscape 7 列 Tab 分隔
        if "\t" in trimmed:
            cols = trimmed.split("\t")
            if len(cols) >= 7:
                k = cols[5].strip() if len(cols) > 5 else ""
                v = cols[6].strip() if len(cols) > 6 else ""
                if k and v and k.lower() not in ignored_keys:
                    parsed[k] = v
                continue

        # 标准 KV：name=value
        if "=" in trimmed:
            k, _, v = trimmed.partition("=")
            k = k.strip()
            v = v.strip()
            k = re.sub(r"^cookie:\s*", "", k, flags=re.IGNORECASE)
            if (v.startswith('"') and v.endswith('"')) or (
                v.startswith("'") and v.endswith("'")
            ):
                v = v[1:-1]
            if k and v and k.lower() not in ignored_keys:
                parsed[k] = v

    return parsed


def calculate_sapisid_hash(
    cookies: dict[str, str], origin: str = DEFAULT_ORIGIN
) -> str:
    """计算 Google SAPISIDHASH 鉴权头部。"""
    sapisid = cookies.get("SAPISID") or cookies.get("__Secure-1PAPISID") or ""
    sapisid1p = cookies.get("__Secure-1PAPISID") or sapisid
    sapisid3p = cookies.get("__Secure-3PAPISID") or sapisid
    if not sapisid:
        return ""

    ts = int(time.time())

    def calc_hash(val: str) -> str:
        return hashlib.sha1(f"{ts} {val} {origin}".encode()).hexdigest()

    h1 = f"SAPISIDHASH {ts}_{calc_hash(sapisid)}"
    h2 = f"SAPISID1PHASH {ts}_{calc_hash(sapisid1p)}"
    h3 = f"SAPISID3PHASH {ts}_{calc_hash(sapisid3p)}"
    return f"{h1} {h2} {h3}"


def build_google_cookie_list(
    pairs: Iterable[tuple[str, str]] | dict[str, str],
    *,
    allow_url_targets: bool = False,
    discard_transient: bool = False,
) -> list[dict[str, object]]:
    """Build Playwright/CDP compatible cookies from name/value pairs."""
    now = int(time.time())
    default_expires = now + 86400 * 180  # 180 天后过期

    seen: set[tuple[str, str, str]] = set()
    cookies: list[dict[str, object]] = []

    def _add_cookie(name: str, value: str, target: str) -> None:
        is_host_target = not target.startswith(".")

        if name.startswith("__Host-") and not allow_url_targets:
            return

        target_kind = "url" if allow_url_targets and is_host_target else "domain"
        target_value = f"https://{target}/" if target_kind == "url" else target
        key = (name, target_kind, target_value)
        if key in seen:
            return
        seen.add(key)

        cookie: dict[str, object] = {
            "name": name,
            "value": value,
            "secure": True,
            "httpOnly": name not in _AUTH_COOKIE_NAMES,
            "sameSite": "None",
            "expires": default_expires,
        }
        if target_kind == "url":
            cookie["url"] = target_value
        else:
            cookie["domain"] = target
            cookie["path"] = "/"
        cookies.append(cookie)

    item_list: list[tuple[str, str]] = (
        [(str(k), str(v)) for k, v in pairs.items()]
        if isinstance(pairs, dict)
        else [(str(k), str(v)) for k, v in pairs]
    )
    for name, value in item_list:
        if discard_transient and (
            name in DISCARDABLE_IMPORT_COOKIE_NAMES
            or name.startswith(("_ga_", "_gcl_"))
        ):
            continue
        targets = _DOMAIN_OVERRIDES.get(name)
        if not targets:
            targets = [".google.com"]
        for target in targets:
            _add_cookie(name, value, target)

    return cookies


def parse_cookie_string(
    raw: object, *, discard_transient: bool = True
) -> dict[str, object]:
    """将任意格式 cookie 解析为 storage state dict，包含 cookies 和 origins 字段。"""
    cookie_dict = parse_raw_cookies(raw)
    return {
        "cookies": build_google_cookie_list(
            cookie_dict, allow_url_targets=False, discard_transient=discard_transient
        ),
        "origins": [],
    }


# 会话必需 Cookie：只有签名票（SAPISID 系）和 SID 是硬性要求——没有签名票
# 根本算不出 SAPISIDHASH。`__Secure-*PSID` 只作提示：部分环境（Chrome 分区
# Cookie）扩展 API 读不到它们，但会话本身仍然有效，不能据此拒绝推送。
SESSION_SIGNING_COOKIE_NAMES = ("SAPISID", "__Secure-1PAPISID", "__Secure-3PAPISID")
SESSION_SESSION_COOKIE_NAMES = ("__Secure-1PSID", "__Secure-3PSID")

_SAME_SITE_ALIASES = {
    "no_restriction": "None",
    "none": "None",
    "lax": "Lax",
    "strict": "Strict",
}


def is_google_cookie_domain(domain: str | None) -> bool:
    """判断 Cookie 域是否属于 google.com（含所有子域，允许前导点）。"""
    clean = str(domain or "").strip().lower().lstrip(".")
    if not clean:
        return False
    return clean == "google.com" or clean.endswith(".google.com")


def _as_cookie_bool(value: object, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "1", "yes"):
            return True
        if lowered in ("false", "0", "no"):
            return False
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _normalize_same_site(value: object) -> str | None:
    if value is None:
        return None
    return _SAME_SITE_ALIASES.get(str(value).strip().lower())


def build_storage_state_from_browser_cookies(
    items: Iterable[dict[str, object]],
    *,
    discard_transient: bool = True,
) -> dict[str, object]:
    """把浏览器扩展（GM_cookie / Cookie-Editor）导出的 Cookie 列表归一化为 storage state。

    与 `build_google_cookie_list` 的区别：这里保留浏览器里每个 Cookie 真实的
    domain / path / expires / sameSite，而不是按名字猜目标域。因为推送来源是
    真实浏览器的完整 Cookie 罐，按原样落盘再灌回 Camoufox 才能还原同一会话。
    """
    cookies: list[dict[str, object]] = []
    seen: set[tuple[str, str, str]] = set()

    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        value = item.get("value")
        if not name or value is None:
            continue
        domain = str(item.get("domain") or "").strip()
        if not is_google_cookie_domain(domain):
            continue
        if discard_transient and (
            name in DISCARDABLE_IMPORT_COOKIE_NAMES or name.startswith(("_ga", "_gcl"))
        ):
            continue
        path = str(item.get("path") or "/").strip() or "/"
        key = (name, domain.lower(), path)
        if key in seen:
            continue
        seen.add(key)

        expires = item.get("expirationDate", item.get("expires"))
        expires_val = -1.0
        if isinstance(expires, (int, float)):
            expires_val = float(expires)
        elif isinstance(expires, str):
            with contextlib.suppress(ValueError):
                expires_val = float(expires)

        cookie: dict[str, object] = {
            "name": name,
            "value": str(value),
            "domain": domain,
            "path": path,
            "expires": expires_val,
            "secure": _as_cookie_bool(item.get("secure"), default=True),
            "httpOnly": _as_cookie_bool(
                item.get("httpOnly"), default=name not in _AUTH_COOKIE_NAMES
            ),
        }
        same_site = _normalize_same_site(item.get("sameSite"))
        if same_site:
            cookie["sameSite"] = same_site
        cookies.append(cookie)

    return {"cookies": cookies, "origins": []}


def missing_session_cookies(names: Iterable[str]) -> list[str]:
    """返回缺失的会话必需 Cookie 类别；空列表表示可以尝试使用。

    签名票是硬性要求：没有 SAPISID 系 Cookie 就无法生成 SAPISIDHASH。
    长寿命会话凭据则接受 `SID` 或 `__Secure-*PSID` 任一——指纹浏览器和
    GM_cookie 这类抓取路径经常读不到 `SID`，但 `__Secure-1PSID` 完全等价，
    不能据此拒绝一份本来可用的凭据（由服务端 ListModels 探活做最终判定）。
    """
    name_set = set(names)
    missing: list[str] = []
    if "SID" not in name_set and not name_set & set(SESSION_SESSION_COOKIE_NAMES):
        missing.append("SID")
    if not any(name in name_set for name in SESSION_SIGNING_COOKIE_NAMES):
        missing.append("SAPISID")
    return missing


def merge_storage_states(
    base: dict[str, object] | None,
    overlay: dict[str, object],
    *,
    keep_base_names: Iterable[str] = (),
) -> dict[str, object]:
    """按 (name, domain, path) 合并两份 storage state，overlay 覆盖 base。

    用途：浏览器扩展可能读不到分区 Cookie（如 __Secure-1PSID），推送时
    与账号现有 auth.json 合并，避免把浏览器里看不见但依然有效的旧 Cookie
    丢掉；overlay 中的同名 Cookie 总是以新推送的值为准。

    `keep_base_names` 里的**键名**是例外：只要 base 里已经存在这个名字的 Cookie，
    就保留 base 的值，不按 (domain, path) 细分——推送方浏览器里的域名写法常常和
    服务端落盘的不一致，按三元组判断会漏保护。传入 `ROLLING_COOKIE_NAMES`
    即可防止外部推送把服务端的滚动代次回滚到上一代（旧代次被 Google 判定为
    重放 → 401 → 账号被判“登录态失效”）。
    """
    keep = {str(name) for name in keep_base_names}
    merged: dict[tuple[str, str, str], dict[str, object]] = {}
    origins: list[object] = []

    if isinstance(base, dict):
        base_cookies = base.get("cookies")
        if isinstance(base_cookies, list):
            for cookie in base_cookies:
                if isinstance(cookie, dict) and cookie.get("name"):
                    key = (
                        str(cookie.get("name")),
                        str(cookie.get("domain") or "").lower(),
                        str(cookie.get("path") or "/"),
                    )
                    merged[key] = dict(cookie)
        base_origins = base.get("origins")
        if isinstance(base_origins, list):
            origins = list(base_origins)

    base_names = {key[0] for key in merged}
    overlay_cookies = overlay.get("cookies")
    if isinstance(overlay_cookies, list):
        for cookie in overlay_cookies:
            if isinstance(cookie, dict) and cookie.get("name"):
                name = str(cookie.get("name"))
                if name in keep and name in base_names:
                    continue
                key = (
                    name,
                    str(cookie.get("domain") or "").lower(),
                    str(cookie.get("path") or "/"),
                )
                merged[key] = dict(cookie)

    overlay_origins = overlay.get("origins")
    if isinstance(overlay_origins, list) and overlay_origins:
        origins = list(overlay_origins)

    return {"cookies": list(merged.values()), "origins": origins}


# 2000-01-01：早于此的 Expires 一律是“清除 Cookie”写法
_EPOCH_SANITY_SECONDS = 946_684_800

_SET_COOKIE_ATTR_PATTERNS = (
    (re.compile(r"^expires=(.+)$", re.IGNORECASE), "expires"),
    (re.compile(r"^max-age=(-?\d+)$", re.IGNORECASE), "max_age"),
    (re.compile(r"^domain=(.+)$", re.IGNORECASE), "domain"),
    (re.compile(r"^path=(.+)$", re.IGNORECASE), "path"),
    (re.compile(r"^samesite=(\w+)$", re.IGNORECASE), "sameSite"),
)


def apply_set_cookie_lines(
    state: dict[str, object] | None,
    set_cookie_lines: Iterable[str],
) -> tuple[dict[str, object], list[str]]:
    """把响应头里的 `Set-Cookie` 叠加到 storage state 上（只向前，不删除）。

    这是无人值守续命的关键：探活/请求响应里 Google 下发的新一代 PSIDTS /
    SIDCC 必须立刻落盘，否则下一次提交的就是旧代次。返回 (新 state, 变更的
    Cookie 名列表)。

    与上游惯例一致地跳过三类噪声：值为空或 `deleted` 的清除指令、gstatic 域、
    以及 Expires 落在 197x-199x 的“立即过期”写法（那是清 Cookie 用的）。
    """
    base = merge_storage_states(state, {"cookies": [], "origins": []})
    index: dict[tuple[str, str, str], dict[str, object]] = {
        (
            str(cookie.get("name")),
            str(cookie.get("domain") or "").lower(),
            str(cookie.get("path") or "/"),
        ): cookie
        for cookie in base["cookies"]  # type: ignore[union-attr]
        if isinstance(cookie, dict) and cookie.get("name")
    }
    by_name: dict[str, tuple[str, str, str]] = {
        str(cookie["name"]): key for key, cookie in index.items()
    }
    changed: list[str] = []

    for raw in set_cookie_lines:
        if not raw or not isinstance(raw, str):
            continue
        parts = [p.strip() for p in raw.split(";") if p.strip()]
        if not parts or "=" not in parts[0]:
            continue
        name, _, value = parts[0].partition("=")
        name = name.strip()
        value = value.strip().strip('"')
        if not name or not value or value == "deleted":
            continue
        # 遥测 Cookie 不进账号库——它不承载会话，还会白白放大 auth.json
        if name in DISCARDABLE_IMPORT_COOKIE_NAMES or name.startswith(("_ga", "_gcl")):
            continue

        attrs: dict[str, str] = {}
        for token in parts[1:]:
            for pattern, label in _SET_COOKIE_ATTR_PATTERNS:
                matched = pattern.match(token)
                if matched:
                    attrs[label] = matched.group(1)
                    break
        if "Secure" in parts[1:]:
            attrs.setdefault("secure", "1")
        if "HttpOnly" in parts[1:]:
            attrs.setdefault("httpOnly", "1")

        domain = attrs.get("domain", ".google.com").strip().lstrip(".")
        if "gstatic" in domain.lower():
            continue
        path = attrs.get("path", "/").strip() or "/"

        expires_val: float = -1.0
        if "max_age" in attrs:
            max_age = float(attrs["max_age"])
            if max_age < 0:
                continue
            expires_val = time.time() + max_age
        elif "expires" in attrs:
            with contextlib.suppress(ValueError, TypeError):
                parsed = parsedate_to_datetime(attrs["expires"])
                if parsed is not None:
                    expires_val = parsed.timestamp()
                    # 2000 年以前视为“清除 Cookie”写法，不是新的会话值
                    if expires_val < _EPOCH_SANITY_SECONDS:
                        continue

        key = (name, domain.lower(), path)
        existing_key = by_name.get(name)
        if existing_key is not None and existing_key != key:
            # 同名不同域视为同一会话值：以已有的那条为准，避免注入重复代次
            key = existing_key
        current = index.get(key)
        if current is not None and current.get("value") == value:
            continue
        entry: dict[str, object] = {
            "name": name,
            "value": value,
            "domain": domain if domain else ".google.com",
            "path": path,
            "expires": expires_val,
            "secure": True,
            "httpOnly": "1" in attrs,
        }
        same_site = _normalize_same_site(attrs.get("sameSite"))
        entry["sameSite"] = same_site or ("None" if entry["httpOnly"] else "Lax")
        index[key] = entry
        by_name[name] = key
        changed.append(name)

    return {"cookies": list(index.values()), "origins": base["origins"]}, changed


def parse_emails_from_ogs_html(
    html_text: str, default_auth_user: str = "0"
) -> dict[str, str]:
    """从 Google OGS Widget 或 AI Studio HTML 中提取 u/{index} 对应的真实邮箱字典。"""
    mapping: dict[str, str] = {}
    if not html_text:
        return mapping
    m_primary = re.search(r'"oPEP7c"\s*:\s*"([^"@\s]+@[^"\s]+)"', html_text)
    if m_primary:
        mapping[str(default_auth_user)] = m_primary.group(1).strip().lower()
    for m in re.finditer(
        r'data-au="(\d+)"\s+data-email="([^"@\s]+@[^"\s]+)"', html_text
    ):
        mapping[m.group(1)] = m.group(2).strip().lower()
    for m in re.finditer(
        r'data-email="([^"@\s]+@[^"\s]+)"\s+data-au="(\d+)"', html_text
    ):
        mapping[m.group(2)] = m.group(1).strip().lower()
    return mapping


async def probe_account_emails_map(
    raw_or_dict: object,
    auth_user: str = "0",
) -> dict[str, str]:
    """通过 Google OneGoogleWidget / AI Studio 页面探测全部已登录子账号的邮箱映射。"""
    cookie_dict = (
        raw_or_dict
        if isinstance(raw_or_dict, dict)
        and all(isinstance(v, str) for v in raw_or_dict.values())
        else parse_raw_cookies(raw_or_dict)
    )
    if not cookie_dict:
        return {}

    import httpx

    from aistudio_api.config import settings

    u_index = str(auth_user or "0")
    cookie_header = "; ".join(f"{k}={v}" for k, v in cookie_dict.items())
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
        "Cookie": cookie_header,
    }
    try:
        async with httpx.AsyncClient(
            timeout=8.0, proxy=settings.proxy_url, follow_redirects=True
        ) as client:
            resp = await client.get(
                f"https://ogs.google.com/u/{u_index}/widget/account",
                headers=headers,
            )
            if resp.status_code == 200:
                emails = parse_emails_from_ogs_html(resp.text, u_index)
                if emails:
                    return emails
            resp_ai = await client.get(
                f"https://aistudio.google.com/u/{u_index}/prompts/new_chat",
                headers=headers,
            )
            if resp_ai.status_code == 200:
                return parse_emails_from_ogs_html(resp_ai.text, u_index)
    except Exception as e:
        logger.debug("通过 HTTP 探测子账号 u/%s 邮箱失败: %s", u_index, e)
    return {}


async def probe_account_email(
    raw_or_dict: object,
    auth_user: str = "0",
) -> str | None:
    """探测单个指定 auth_user 的登录邮箱。"""
    u_index = str(auth_user or "0")
    emails_map = await probe_account_emails_map(raw_or_dict, auth_user=u_index)
    if u_index in emails_map:
        return emails_map[u_index]
    if u_index != "0":
        root_map = await probe_account_emails_map(raw_or_dict, auth_user="0")
        if u_index in root_map:
            return root_map[u_index]
    return None


async def probe_account_benefit_tier(
    raw_or_dict: object,
    auth_user: str = "0",
) -> str:
    """通过 GetAiStudioBenefitTier RPC 探测指定子账号的订阅权益等级。"""
    cookie_dict = (
        raw_or_dict
        if isinstance(raw_or_dict, dict)
        and all(isinstance(v, str) for v in raw_or_dict.values())
        else parse_raw_cookies(raw_or_dict)
    )
    if not cookie_dict:
        return "Free"

    import httpx

    from aistudio_api.config import settings

    u_index = str(auth_user or "0")
    auth_header = calculate_sapisid_hash(cookie_dict)
    cookie_header = "; ".join(f"{k}={v}" for k, v in cookie_dict.items())
    headers = {
        "Content-Type": "application/json+protobuf",
        "X-User-Agent": DEFAULT_USER_AGENT,
        "X-Goog-Api-Key": DEFAULT_API_KEY,
        "X-Goog-AuthUser": u_index,
        "X-Goog-Ext-519733851-bin": DEFAULT_EXT_BIN,
        "X-AIStudio-Visit-Id": f"v1_{uuid.uuid4()}",
        "Origin": DEFAULT_ORIGIN,
        "Referer": f"{DEFAULT_ORIGIN}/",
        "Cookie": cookie_header,
    }
    if auth_header:
        headers["Authorization"] = auth_header

    tier_url = "https://alkalimakersuite-pa.clients6.google.com/$rpc/google.internal.alkali.applications.makersuite.v1.MakerSuiteService/GetAiStudioBenefitTier"
    try:
        async with httpx.AsyncClient(timeout=8.0, proxy=settings.proxy_url) as client:
            resp = await client.post(tier_url, headers=headers, content="[]")
            if resp.status_code == 200:
                return parse_benefit_tier_response(resp.text)
    except Exception as e:
        logger.debug("探测子账号 u/%s 权益等级失败: %s", u_index, e)
    return "Free"


async def probe_google_accounts_infinite(
    raw_or_dict: object,
    *,
    max_fails_in_a_row: int = 1,
) -> list[dict[str, object]]:
    """一路向下探测多账号登录索引（u/0, u/1, u/2...），直到探测失败为止。

    Returns:
        有效账号列表，例如 [{"auth_user": "0", "status_code": 200, "name": "Google Account (u/0)"}, ...]
    """
    # 推送路径传进来的是 {name: value} 字典；parse_raw_cookies 只认字符串 /
    # Cookie-Editor 结构，对裸字典会退化成按 "=" 拆 JSON 文本，结果为空。
    # 与 probe_account_emails_map 保持同一套判断，否则推送侧永远探不到子号。
    cookie_dict = (
        raw_or_dict
        if isinstance(raw_or_dict, dict)
        and all(isinstance(v, str) for v in raw_or_dict.values())
        else parse_raw_cookies(raw_or_dict)
    )
    if not cookie_dict:
        return []

    import httpx

    from aistudio_api.config import settings

    cookie_header = "; ".join(f"{k}={v}" for k, v in cookie_dict.items())
    valid_accounts: list[dict[str, object]] = []

    auth_user_idx = 0
    consecutive_failures = 0
    emails_map = await probe_account_emails_map(cookie_dict, auth_user="0")
    async with httpx.AsyncClient(timeout=10.0, proxy=settings.proxy_url) as client:
        while True:
            u_index = str(auth_user_idx)
            auth_header = calculate_sapisid_hash(cookie_dict)
            headers = {
                "Content-Type": "application/json+protobuf",
                "X-User-Agent": DEFAULT_USER_AGENT,
                "X-Goog-Api-Key": DEFAULT_API_KEY,
                "X-Goog-AuthUser": u_index,
                "X-Goog-Ext-519733851-bin": DEFAULT_EXT_BIN,
                "X-AIStudio-Visit-Id": f"v1_{uuid.uuid4()}",
                "Origin": DEFAULT_ORIGIN,
                "Referer": f"{DEFAULT_ORIGIN}/",
                "Cookie": cookie_header,
            }
            if auth_header:
                headers["Authorization"] = auth_header

            url = "https://alkalimakersuite-pa.clients6.google.com/$rpc/google.internal.alkali.applications.makersuite.v1.MakerSuiteService/ListModels"
            try:
                resp = await client.post(url, headers=headers, content="[]")
                # 200 (OK), 400 (Bad Request on param but authed), 403 (Forbidden/Permission/Quota) 均表示此账号 Session 存在
                # 401 表示该 auth_user 未登录或 Session 无效
                if resp.status_code in (200, 400, 403):
                    consecutive_failures = 0
                    detected_tier = "Free"
                    tier_url = "https://alkalimakersuite-pa.clients6.google.com/$rpc/google.internal.alkali.applications.makersuite.v1.MakerSuiteService/GetAiStudioBenefitTier"
                    try:
                        tier_resp = await client.post(
                            tier_url, headers=headers, content="[]"
                        )
                        if tier_resp.status_code == 200:
                            detected_tier = parse_benefit_tier_response(tier_resp.text)
                    except Exception:
                        pass
                    detected_email = emails_map.get(u_index)
                    if not detected_email:
                        try:
                            extra_map = await probe_account_emails_map(
                                cookie_dict, auth_user=u_index
                            )
                            emails_map.update(extra_map)
                            detected_email = emails_map.get(u_index)
                        except Exception:
                            pass
                    valid_accounts.append(
                        {
                            "auth_user": u_index,
                            "status_code": resp.status_code,
                            "name": f"Google Account (u/{u_index})",
                            "email": detected_email,
                            "tier": detected_tier,
                        }
                    )
                    logger.info(
                        "子账号 u/%s 探活成功 (HTTP %d, email=%s, tier=%s)",
                        u_index,
                        resp.status_code,
                        detected_email or "unknown",
                        detected_tier,
                    )
                elif resp.status_code == 401:
                    consecutive_failures += 1
                    logger.debug("子账号 u/%s 探活返回 401 (未授权)", u_index)
                else:
                    consecutive_failures += 1
                    logger.debug(
                        "子账号 u/%s 探活返回 HTTP %d", u_index, resp.status_code
                    )
            except Exception as e:
                consecutive_failures += 1
                logger.debug("子账号 u/%s 探活异常: %s", u_index, e)

            if consecutive_failures >= max_fails_in_a_row:
                break

            auth_user_idx += 1

    return valid_accounts
