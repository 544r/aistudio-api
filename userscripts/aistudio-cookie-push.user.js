// ==UserScript==
// @name         AIStudio API Cookie Pusher
// @namespace    aistudio-api-push
// @version      1.1.0
// @description  定时把 AI Studio 页面上的最新 Cookie 推送到自建 aistudio-api 服务，服务器自动覆盖账号凭证并热注入浏览器，替代"手动粘贴导入、过期再重贴"
// @author       aistudio-api
// @match        https://aistudio.google.com/*
// @match        https://alkalimakersuite-pa.clients6.google.com/*
// @run-at       document-idle
// @noframes
// @updateURL    https://raw.githubusercontent.com/544r/aistudio-api/master/userscripts/aistudio-cookie-push.user.js
// @downloadURL  https://raw.githubusercontent.com/544r/aistudio-api/master/userscripts/aistudio-cookie-push.user.js
// @grant        unsafeWindow
// @grant        GM_cookie
// @grant        GM_xmlhttpRequest
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_registerMenuCommand
// @connect      *
// ==/UserScript==

/*
 * 用法：
 * 1. 服务器地址与管理密码：篡改猴 菜单 → 「设置服务器地址」「设置管理密码」。
 *    管理密码就是 aistudio-api 管理控制台的登录密码（未设置则留空）。
 * 2. 篡改猴 需把「允许脚本访问 Cookie」设为 all，否则读不到 HttpOnly 的
 *    会话 Cookie：管理面板 → 设置 → 配置模式选「高级」→ 安全栏 → 设为 all → 保存。
 * 3. 首次推送时 篡改猴 会弹出"连接到 xxx"的授权框，选「总是允许」。
 * 4. 多账号浏览器建议点「✅ 设置允许推送的账号」，填入小号邮箱：停在主号
 *    页面（常见于默认的 /u/0/）时自动推送会直接跳过，不会把主号传上去。
 * 5. 之后保持本页（或任意 AI Studio 页）开着即可：页面加载后自动推一次，
 *    之后按设定间隔自动重推（默认 60 分钟）；右下角胶囊显示最近一次结果，
 *    点击胶囊立即手动推送。
 * 6. 长期挂机可选两个保护（菜单里开关，默认关闭）：
 *    · 「🔊 静音音频保活」：循环播放静音音轨，防止标签页被浏览器冻结/丢弃
 *      （被丢弃后定时推送会一起停）；
 *    · 「🔄 设置自动刷新间隔」：每 N 分钟在**标签页不可见时**整页刷新一次，
 *      让页面重新发请求、轮换 Cookie，刷新后会自动再推送一次。
 *
 * 说明：
 * - 推送的是完整 Google 会话 Cookie 罐（含 SID / SAPISID / __Secure-*），
 *   仅发送到你配置的服务器，请使用可信的自建地址。
 * - 后台标签页的主线程定时器会被浏览器节流，这里用 Worker 定时器绕开
 *   节流（与 catiecli 脚本的保活方案一致）；页面被系统冻结时会在恢复
 *   可见后按"已超时"补推一次。
 */

(function () {
    "use strict";

    // 页面原始 window：Trusted Types 策略对象必须由页面上下文创建
    const W = typeof unsafeWindow !== "undefined" ? unsafeWindow : window;

    // ==================== 常量 ====================

    // GenerateContent 端点：GM_cookie 按这个 URL 查询 = 浏览器实际发送的 Cookie 头（含 HttpOnly）
    const GENERATE_URL =
        "https://alkalimakersuite-pa.clients6.google.com/$rpc/google.internal.alkali.applications.makersuite.v1.MakerSuiteService/GenerateContent";
    // 页面主域：补漏会话 Cookie
    const PAGE_URL = "https://aistudio.google.com/prompts/";

    // 会话必需：缺任一类上游必然 401，推送前就拦下
    const REQUIRED_SIGNING = ["SAPISID", "__Secure-1PAPISID", "__Secure-3PAPISID"];
    const REQUIRED_SESSION = ["__Secure-1PSID", "__Secure-3PSID"];
    const CORE_PROBE_NAMES = [
        "SAPISID",
        "__Secure-1PAPISID",
        "__Secure-3PAPISID",
        "__Secure-1PSID",
        "__Secure-3PSID",
        "SID",
        "HSID",
        "SSID",
    ];

    const DEFAULT_INTERVAL_MIN = 60;
    const PUSH_TIMEOUT_MS = 30000;
    const INITIAL_PUSH_DELAY_MS = 5000;
    // 自动刷新：默认关闭，最少 30 分钟一次
    const DEFAULT_REFRESH_MIN = 0;
    const MIN_REFRESH_MIN = 30;

    const STORE_SERVER = "ap_server";
    const STORE_PASSWORD = "ap_password";
    const STORE_INTERVAL = "ap_interval_min";
    const STORE_ALLOWED_EMAILS = "ap_allowed_emails";
    const STORE_AUDIO_KEEPALIVE = "ap_audio_keepalive";
    const STORE_REFRESH_MIN = "ap_refresh_min";
    const STORE_LAST_REFRESH = "ap_last_refresh";

    // ==================== 配置 ====================

    const cfg = {
        server: String(GM_getValue(STORE_SERVER, "") || "").trim(),
        password: String(GM_getValue(STORE_PASSWORD, "") || ""),
        intervalMin: Number(GM_getValue(STORE_INTERVAL, DEFAULT_INTERVAL_MIN)) || DEFAULT_INTERVAL_MIN,
        allowedEmails: String(GM_getValue(STORE_ALLOWED_EMAILS, "") || "")
            .split(",")
            .map((s) => s.trim().toLowerCase())
            .filter((s) => s.includes("@")),
        audioKeepAlive: GM_getValue(STORE_AUDIO_KEEPALIVE, false) === true,
        refreshMin: Number(GM_getValue(STORE_REFRESH_MIN, DEFAULT_REFRESH_MIN)) || 0,
    };

    function normalizeServer(raw) {
        let s = String(raw || "").trim();
        if (!s) return "";
        if (!/^https?:\/\//i.test(s)) s = "http://" + s;
        return s.replace(/\/+$/, "");
    }

    function saveServer() {
        const input = prompt("aistudio-api 服务器地址（如 http://192.168.1.10:8000）：", cfg.server || "http://");
        if (input === null) return;
        cfg.server = normalizeServer(input);
        GM_setValue(STORE_SERVER, cfg.server);
        log(`✅ 服务器地址已保存: ${cfg.server || "（空）"}`);
        setBadge("idle", "点击立即推送");
    }

    function savePassword() {
        const input = prompt(
            "管理控制台密码（作为 x-api-key 推送；服务器未启用密码则留空）：",
            cfg.password || ""
        );
        if (input === null) return;
        cfg.password = input;
        GM_setValue(STORE_PASSWORD, cfg.password);
        log("✅ 管理密码已保存");
    }

    function saveInterval() {
        const input = prompt(
            "自动推送间隔（分钟，建议 30-120）：",
            String(cfg.intervalMin)
        );
        if (input === null) return;
        const val = Math.max(5, Math.min(24 * 60, parseInt(input, 10) || DEFAULT_INTERVAL_MIN));
        cfg.intervalMin = val;
        GM_setValue(STORE_INTERVAL, val);
        log(`✅ 推送间隔已设为 ${val} 分钟（刷新页面后生效）`);
    }

    function saveAllowedEmails() {
        const input = prompt(
            "只允许推送这些账号（逗号分隔的邮箱；留空 = 不限制）。\n" +
                "多账号浏览器建议填小号邮箱，防止在主号页面误推：",
            cfg.allowedEmails.join(", ")
        );
        if (input === null) return;
        cfg.allowedEmails = input
            .split(",")
            .map((s) => s.trim().toLowerCase())
            .filter((s) => s.includes("@"));
        GM_setValue(STORE_ALLOWED_EMAILS, cfg.allowedEmails.join(","));
        log(
            cfg.allowedEmails.length
                ? `✅ 仅推送这些账号: ${cfg.allowedEmails.join(", ")}`
                : "✅ 已取消账号限制（任意账号都会推送）"
        );
    }

    function saveRefreshInterval() {
        const input = prompt(
            "自动刷新间隔（分钟，0 = 关闭，最小 30）。\n" +
                "刷新只在标签页不可见时执行，用于让页面重新发请求、轮换 Cookie：",
            String(cfg.refreshMin)
        );
        if (input === null) return;
        const raw = parseInt(input, 10) || 0;
        cfg.refreshMin = raw <= 0 ? 0 : Math.max(MIN_REFRESH_MIN, Math.min(24 * 60, raw));
        GM_setValue(STORE_REFRESH_MIN, cfg.refreshMin);
        log(
            cfg.refreshMin
                ? `✅ 自动刷新已设为每 ${cfg.refreshMin} 分钟（刷新页面后生效）`
                : "✅ 已关闭自动刷新"
        );
    }

    function toggleAudioKeepAlive() {
        if (audioKeepAliveActive()) {
            stopAudioKeepAlive();
            GM_setValue(STORE_AUDIO_KEEPALIVE, false);
            log("🔴 音频保活已关闭");
            return;
        }
        const ok = startAudioKeepAlive();
        GM_setValue(STORE_AUDIO_KEEPALIVE, ok);
    }

    GM_registerMenuCommand("📤 立即推送 Cookie", () => pushNow("手动"));
    GM_registerMenuCommand("⚙ 设置服务器地址", saveServer);
    GM_registerMenuCommand("🔑 设置管理密码", savePassword);
    GM_registerMenuCommand("⏱ 设置推送间隔", saveInterval);
    GM_registerMenuCommand("✅ 设置允许推送的账号", saveAllowedEmails);
    GM_registerMenuCommand("🔊 静音音频保活 开/关", toggleAudioKeepAlive);
    GM_registerMenuCommand("🔄 设置自动刷新间隔", saveRefreshInterval);
    GM_registerMenuCommand("ℹ 查看状态", () => {
        alert(
            `服务器: ${cfg.server || "未配置"}\n` +
                `间隔: ${cfg.intervalMin} 分钟\n` +
                `账号限制: ${cfg.allowedEmails.length ? cfg.allowedEmails.join(", ") : "不限制"}\n` +
                `音频保活: ${audioKeepAliveActive() ? "开启" : "关闭"}\n` +
                `自动刷新: ${cfg.refreshMin ? `每 ${cfg.refreshMin} 分钟` : "关闭"}\n` +
                `最近推送: ${state.lastPushText || "尚未推送"}\n` +
                `最近结果: ${state.lastResult || "-"}`
        );
    });

    // ==================== 状态与工具 ====================

    const state = {
        pushing: false,
        lastPushAt: 0,
        lastPushText: "",
        lastResult: "",
        timer: null,
        refreshTimer: null,
        fallbackTimer: null,
    };

    let badgeEl = null;

    function log(msg) {
        console.log("[AIStudio-Push]", msg);
    }

    function gmCookieList(query) {
        return new Promise((resolve) => {
            try {
                if (typeof GM_cookie !== "undefined" && typeof GM_cookie.list === "function") {
                    GM_cookie.list(query, (cookies, error) => {
                        resolve({ cookies: cookies || [], error: error || null });
                    });
                    return;
                }
                if (
                    typeof GM !== "undefined" &&
                    GM.cookie &&
                    typeof GM.cookie.list === "function"
                ) {
                    GM.cookie
                        .list(query)
                        .then((cookies) => resolve({ cookies: cookies || [], error: null }))
                        .catch((e) => resolve({ cookies: [], error: e }));
                    return;
                }
            } catch (e) {
                resolve({ cookies: [], error: e });
                return;
            }
            resolve({ cookies: [], error: "当前脚本管理器没有 Cookie API（GM_cookie）" });
        });
    }

    function isGoogleDomain(domain) {
        const d = String(domain || "").replace(/^\./, "").toLowerCase();
        return d === "google.com" || d.endsWith(".google.com");
    }

    function accountPathPrefix() {
        const m = location.pathname.match(/^\/u\/(\d+)\//);
        return m ? m[1] : "0";
    }

    function detectEmail() {
        try {
            const um = location.search.match(/[?&]Email=([^&]+)/);
            if (um) return decodeURIComponent(um[1]).toLowerCase();
        } catch (_) {}
        try {
            for (const el of document.querySelectorAll("[data-email]")) {
                const m = (el.getAttribute("data-email") || "").match(/[\w.+-]+@[\w.-]+\.\w+/);
                if (m) return m[0].toLowerCase();
            }
        } catch (_) {}
        try {
            for (const link of document.querySelectorAll("a[aria-label]")) {
                const m = (link.getAttribute("aria-label") || "").match(/[\w.+-]+@[\w.-]+\.\w+/);
                if (m) return m[0].toLowerCase();
            }
        } catch (_) {}
        return "";
    }

    // ==================== Cookie 收集 ====================

    // 三重查询后按 (name|domain|path) 去重：这三元组才是一个 Cookie 的真实身份，
    // 同一名字在不同域/路径上是不同 Cookie（如 __Secure-1PSIDTS），不能按名收敛。
    async function collectCookies() {
        const byKey = new Map();
        const addCookie = (c, source) => {
            if (!c || !c.name || !isGoogleDomain(c.domain)) return;
            const key = `${c.name}|${c.domain}|${(c.path || "/").toLowerCase()}`;
            const prev = byKey.get(key);
            if (!prev) {
                byKey.set(key, { ...c, source });
                return;
            }
            // 同三元组重复：请求端点结果优先（与浏览器实际发送语义一致）
            if (prev.source !== "reqURL" && source === "reqURL") {
                byKey.set(key, { ...c, source });
            }
        };

        // ① 按 GenerateContent 端点 URL 查询
        const req = await gmCookieList({ url: GENERATE_URL });
        if (!req.error) req.cookies.forEach((c) => addCookie(c, "reqURL"));
        else log(`⚠ 端点 URL 查询失败: ${String(req.error).slice(0, 80)}`);

        // ② 按页面 URL 补漏
        const page = await gmCookieList({ url: PAGE_URL });
        if (!page.error) page.cookies.forEach((c) => addCookie(c, "pageURL"));

        // ③ 核心 Cookie 缺失时按 name 补查（个别管理器不支持组合查询）
        const names = new Set([...byKey.values()].map((c) => c.name));
        const missing = CORE_PROBE_NAMES.filter((n) => !names.has(n));
        for (const name of missing) {
            const res = await gmCookieList({ name });
            if (res.error) continue;
            res.cookies.filter((c) => isGoogleDomain(c.domain)).forEach((c) => addCookie(c, "byName"));
        }

        const finalCookies = [...byKey.values()].map(({ source, ...rest }) => rest);
        log(
            `Cookie 收集: 端点=${req.error ? "失败" : req.cookies.length} | ` +
                `页面=${page.error ? "失败" : page.cookies.length} | 合计 ${finalCookies.length} 条`
        );
        return finalCookies;
    }

    // 硬性要求只有 SID 与签名票：没有 SAPISID 系 Cookie 就算不出 SAPISIDHASH。
    // __Secure-*PSID 在 Chrome 分区 Cookie 场景下可能读不到，但会话仍然有效，
    // 只作提示，交给服务端合并旧 Cookie 并用 ListModels 做最终判定。
    function missingSessionCookies(cookies) {
        const names = new Set(cookies.map((c) => c.name));
        const missing = [];
        if (!names.has("SID")) missing.push("SID");
        if (!REQUIRED_SIGNING.some((n) => names.has(n))) missing.push("SAPISID");
        return missing;
    }

    // ==================== 推送 ====================

    function gmPost(url, headers, body) {
        return new Promise((resolve, reject) => {
            try {
                GM_xmlhttpRequest({
                    method: "POST",
                    url,
                    headers,
                    data: body,
                    timeout: PUSH_TIMEOUT_MS,
                    onload: (resp) => resolve(resp),
                    onerror: () => reject(new Error("网络错误")),
                    ontimeout: () => reject(new Error("请求超时")),
                });
            } catch (e) {
                reject(e);
            }
        });
    }

    async function pushNow(reason) {
        if (state.pushing) return;
        if (!cfg.server) {
            log("⚠ 尚未配置服务器地址（篡改猴 菜单 → 「设置服务器地址」）");
            setBadge("error", "未配置服务器地址");
            return;
        }
        if (!location.hostname.endsWith("aistudio.google.com")) return;

        state.pushing = true;
        setBadge("busy", "推送中…");
        try {
            const cookies = await collectCookies();
            const missing = missingSessionCookies(cookies);
            if (missing.length) {
                log(
                    `❌ 缺少会话必需 Cookie: ${missing.join(", ")}（已收集: ` +
                        cookies.map((c) => c.name).join(", ") +
                        "）。请把 篡改猴「允许脚本访问 Cookie」设为 all（设置 → 高级 → 安全栏）后刷新重试"
                );
                setBadge("error", `缺 Cookie: ${missing.join("/")}`);
                return;
            }
            if (!REQUIRED_SESSION.some((n) => cookies.some((c) => c.name === n))) {
                log(
                    "⚠ 未收集到 __Secure-1PSID/3PSID（Chrome 分区 Cookie 常见），继续推送；" +
                        "服务端会与账号现有 Cookie 合并并校验登录态"
                );
            }

            const detectedEmail = detectEmail().toLowerCase();
            // 账号白名单：填了就只推白名单里的账号。多账号浏览器里主号常是
            // 默认页（/u/0/），一旦忘记切换页面自动推送就会把主号也传上去，
            // 这里从源头拦住；识别不到邮箱时宁可跳过（fail closed）。
            if (cfg.allowedEmails.length) {
                if (!detectedEmail) {
                    log(
                        "⚠ 无法识别当前页面账号邮箱，白名单模式下已跳过本次推送。" +
                            "请在该账号的 AI Studio 页面手动点「立即推送」，或清空白名单"
                    );
                    setBadge("error", "无法识别账号，已跳过");
                    return;
                }
                if (!cfg.allowedEmails.includes(detectedEmail)) {
                    log(`⚠ 当前账号 ${detectedEmail} 不在白名单内，已跳过推送`);
                    setBadge("error", "账号不在白名单");
                    return;
                }
            }

            const payload = {
                cookies,
                auth_user: accountPathPrefix(),
                email: detectedEmail || undefined,
                source: "userscript",
            };
            const headers = { "Content-Type": "application/json" };
            // 密码同时走 header 与 query：header 只允许 Latin-1，中文/特殊字符密码
            // 在部分管理器里会被拒绝，query 经 URL 编码没有这个限制（服务端两种都认）
            let pushUrl = `${cfg.server}/api/accounts/push-cookies`;
            if (cfg.password) {
                headers["x-api-key"] = cfg.password;
                pushUrl += `?key=${encodeURIComponent(cfg.password)}`;
            }

            log(`☁ 开始推送（${reason}）：${cookies.length} 条 Cookie，u/${payload.auth_user}`);
            const resp = await gmPost(
                pushUrl,
                headers,
                JSON.stringify(payload)
            );
            let data = {};
            try {
                data = JSON.parse(resp.responseText);
            } catch (_) {}

            if (resp.status === 200 && data.account_id) {
                const validText =
                    data.session_valid === true
                        ? "登录态有效"
                        : data.session_valid === false
                          ? "登录态已失效"
                          : "登录态未验证";
                const text =
                    `✅ 已推送（${validText}）` +
                    (data.updated_accounts > 1 ? `，整组刷新 ${data.updated_accounts} 个账号` : "");
                log(
                    `${text}：${data.name || data.account_id}` +
                        (data.created ? "（新建账号）" : "") +
                        (data.injected ? "（已热注入浏览器）" : "")
                );
                state.lastPushAt = Date.now();
                state.lastPushText = new Date().toLocaleString("zh-CN", { hour12: false });
                state.lastResult = text;
                setBadge(data.session_valid === false ? "error" : "ok", `${validText} · ${shortTime()}`);
            } else {
                const detail =
                    (data && (data.detail || (data.error && data.error.message))) ||
                    `HTTP ${resp.status}`;
                log(`❌ 推送失败 [${resp.status}]: ${typeof detail === "string" ? detail : JSON.stringify(detail)}`);
                state.lastResult = `失败: ${resp.status}`;
                setBadge("error", `推送失败 ${resp.status}`);
            }
        } catch (e) {
            log(`❌ 推送异常: ${(e && e.message) || e}`);
            state.lastResult = String((e && e.message) || e);
            setBadge("error", "推送异常");
        } finally {
            state.pushing = false;
        }
    }

    // ==================== 状态胶囊 ====================

    function shortTime() {
        const d = new Date();
        return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
    }

    function ensureBadge() {
        if (badgeEl) return badgeEl;
        badgeEl = document.createElement("div");
        badgeEl.id = "aistudio-api-push-badge";
        badgeEl.style.cssText = [
            "position:fixed",
            "right:12px",
            "bottom:12px",
            "z-index:2147483647",
            "padding:6px 10px",
            "border-radius:999px",
            "font:12px/1.4 system-ui,-apple-system,'Segoe UI',sans-serif",
            "color:#fff",
            "background:#3f51b5",
            "box-shadow:0 2px 10px rgba(0,0,0,.25)",
            "cursor:pointer",
            "user-select:none",
            "opacity:.92",
        ].join(";");
        badgeEl.title = "点击立即推送 Cookie";
        badgeEl.addEventListener("click", () => pushNow("手动"));
        document.body.appendChild(badgeEl);
        return badgeEl;
    }

    function setBadge(kind, text) {
        const el = ensureBadge();
        const colors = { idle: "#5c6bc0", ok: "#2e7d32", error: "#c62828", busy: "#1565c0" };
        el.style.background = colors[kind] || colors.idle;
        el.textContent = `AIStudio 推送 · ${text}`;
    }

    // ==================== 音频保活 ====================

    // 构造 1 秒全零采样的 WAV：循环播放时标签页始终处于"正在播放音频"状态，
    // Chrome 不会冻结/丢弃它（标签页一旦被丢弃，定时推送也会跟着停摆）。
    const keepAlive = { el: null, ctx: null, gestureBound: false };

    function makeSilentWavUrl() {
        const sampleRate = 8000;
        const samples = sampleRate;
        const buffer = new ArrayBuffer(44 + samples * 2);
        const view = new DataView(buffer);
        const writeStr = (offset, text) => {
            for (let i = 0; i < text.length; i++) {
                view.setUint8(offset + i, text.charCodeAt(i));
            }
        };
        writeStr(0, "RIFF");
        view.setUint32(4, 36 + samples * 2, true);
        writeStr(8, "WAVE");
        writeStr(12, "fmt ");
        view.setUint32(16, 16, true);
        view.setUint16(20, 1, true);
        view.setUint16(22, 1, true);
        view.setUint32(24, sampleRate, true);
        view.setUint32(28, sampleRate * 2, true);
        view.setUint16(32, 2, true);
        view.setUint16(34, 16, true);
        writeStr(36, "data");
        view.setUint32(40, samples * 2, true);
        return URL.createObjectURL(new Blob([buffer], { type: "audio/wav" }));
    }

    function audioKeepAliveActive() {
        return !!(keepAlive.el || keepAlive.ctx);
    }

    function startAudioKeepAlive() {
        if (audioKeepAliveActive()) return true;
        try {
            const el = document.createElement("audio");
            el.src = makeSilentWavUrl();
            el.loop = true;
            el.volume = 0.01;
            el.style.display = "none";
            document.body.appendChild(el);
            keepAlive.el = el;
            log("✅ 音频保活已开启（静音循环）");
            resumeKeepAliveAudio();
            return true;
        } catch (e) {
            log(`❌ 音频保活开启失败: ${(e && e.message) || e}`);
            return false;
        }
    }

    function stopAudioKeepAlive() {
        if (keepAlive.el) {
            try {
                keepAlive.el.pause();
            } catch (_) {}
            try {
                keepAlive.el.remove();
            } catch (_) {}
            keepAlive.el = null;
        }
        if (keepAlive.ctx) {
            try {
                keepAlive.ctx.close();
            } catch (_) {}
            keepAlive.ctx = null;
        }
    }

    // 自动播放策略会拦截"没有用户手势"的播放：静音轨道通常放行；仍被拦截时
    // 退回 Web Audio 极低增益振荡器，并在首次点击/按键后自动补启。
    function resumeKeepAliveAudio() {
        const el = keepAlive.el;
        if (!el) return;
        const playing = el.play();
        if (playing && typeof playing.catch === "function") {
            playing.catch(() => {
                startWebAudioKeepAlive();
                armGestureRetry();
            });
        }
    }

    function startWebAudioKeepAlive() {
        if (keepAlive.ctx) return;
        try {
            const Ctx = W.AudioContext || W.webkitAudioContext;
            if (!Ctx) return;
            const ctx = new Ctx();
            const osc = ctx.createOscillator();
            const gain = ctx.createGain();
            gain.gain.value = 0.0001; // -80dB，听不见但存在真实音频流
            osc.frequency.value = 440;
            osc.connect(gain);
            gain.connect(ctx.destination);
            osc.start();
            keepAlive.ctx = ctx;
            log("✅ 音频保活已切换为 Web Audio 静音振荡器");
        } catch (e) {
            log(`⚠ Web Audio 保活不可用: ${(e && e.message) || e}`);
        }
    }

    function armGestureRetry() {
        if (keepAlive.gestureBound) return;
        keepAlive.gestureBound = true;
        const retry = () => {
            if (keepAlive.el) keepAlive.el.play().catch(() => {});
            if (keepAlive.ctx && keepAlive.ctx.state === "suspended") {
                keepAlive.ctx.resume().catch(() => {});
            }
        };
        document.addEventListener("pointerdown", retry, { once: true });
        document.addEventListener("keydown", retry, { once: true });
        log(
            "⚠ 音频保活被自动播放策略拦截：在页面点一下即可启用；" +
                "长期挂机建议给浏览器加参数 --autoplay-policy=no-user-gesture-required"
        );
    }

    // ==================== 调度 ====================

    // 后台标签页的主线程定时器会被浏览器节流（隐藏几分钟后压到每分钟/每小时），
    // 这里把定时器放进 Worker 线程绕开节流；Worker 不可用时回退主线程。
    // AI Studio 启用了 Trusted Types，Worker 的 scriptURL 是 TrustedScriptURL
    // 槽位：依次尝试自建策略 → 页面 defaultPolicy → 直接传 URL，都不行回退。
    // Trusted Types 策略只能注册一次：两个定时器共用同一个策略对象
    let ttPolicy = null;

    function trustedWorkerUrl(rawUrl) {
        const tt = W.trustedTypes;
        if (!tt || !tt.createPolicy) return rawUrl;
        if (!ttPolicy) {
            try {
                ttPolicy = tt.createPolicy("aistudio-api-push", {
                    createScriptURL: (s) => s,
                });
            } catch (_) {
                ttPolicy = tt.defaultPolicy || null;
            }
        }
        return ttPolicy ? ttPolicy.createScriptURL(rawUrl) : null;
    }

    function createWorkerTimer(intervalMs, onTick) {
        const fallbackToTimer = () => {
            const timer = setInterval(onTick, intervalMs);
            log(`已回退主线程定时器：每 ${Math.round(intervalMs / 60000)} 分钟`);
            return timer;
        };
        try {
            const src =
                "const t=setInterval(()=>postMessage('tick')," +
                intervalMs +
                ");onmessage=(e)=>{if(e.data==='stop'){clearInterval(t);close()}};";
            const url = URL.createObjectURL(new Blob([src], { type: "text/javascript" }));
            const workerUrl = trustedWorkerUrl(url);
            if (!workerUrl) throw new Error("Trusted Types 锁定，无法构造 Worker 脚本 URL");
            const worker = new Worker(workerUrl);
            worker.onmessage = (ev) => {
                if (ev.data === "tick") onTick();
            };
            worker.onerror = () => {
                log("⚠ Worker 定时器异常，回退主线程定时器");
                fallbackToTimer();
            };
            log(`已启动 Worker 定时器：每 ${Math.round(intervalMs / 60000)} 分钟`);
            return worker;
        } catch (e) {
            log(`⚠ Worker 定时器不可用（${(e && e.message) || e}），回退主线程定时器`);
            fallbackToTimer();
            return null;
        }
    }

    // 定时刷新：让页面重新走一遍加载流程（真实请求 → Google 轮换 Cookie），
    // 刷新后脚本会重新推送一次。只在标签页不可见时执行，避免打断正在使用的页面。
    function maybeRefreshPage() {
        if (!cfg.refreshMin) return;
        if (location.hostname !== "aistudio.google.com") return;
        if (document.visibilityState === "visible") return;
        if (state.pushing) return;
        const last = Number(GM_getValue(STORE_LAST_REFRESH, 0)) || 0;
        const intervalMs = cfg.refreshMin * 60 * 1000;
        if (last && Date.now() - last < intervalMs * 0.9) return;
        GM_setValue(STORE_LAST_REFRESH, Date.now());
        log(
            `🔄 自动刷新页面轮换 Cookie（间隔 ${cfg.refreshMin} 分钟），刷新后会自动再推送一次`
        );
        setTimeout(() => location.reload(), 1500);
    }

    function init() {
        if (!location.hostname.endsWith("aistudio.google.com")) return;

        setBadge("idle", cfg.server ? "准备推送" : "未配置服务器");
        log(`脚本已加载 v1.1.0，服务器: ${cfg.server || "未配置"}`);

        if (!cfg.server) {
            log("⚠ 请先通过 篡改猴 菜单设置服务器地址（「⚙ 设置服务器地址」）");
        } else {
            setTimeout(() => pushNow("页面加载"), INITIAL_PUSH_DELAY_MS);
        }

        state.timer = createWorkerTimer(cfg.intervalMin * 60 * 1000, () =>
            pushNow("定时")
        );
        if (cfg.audioKeepAlive) startAudioKeepAlive();
        if (cfg.refreshMin > 0) {
            state.refreshTimer = createWorkerTimer(
                cfg.refreshMin * 60 * 1000,
                maybeRefreshPage
            );
        }

        document.addEventListener("visibilitychange", () => {
            if (document.visibilityState !== "visible") return;
            const intervalMs = cfg.intervalMin * 60 * 1000;
            if (!state.lastPushAt || Date.now() - state.lastPushAt >= intervalMs) {
                pushNow("标签页恢复可见");
            }
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init, { once: true });
    } else {
        init();
    }
})();
