(args) => {
    try {
        const EMAIL_RE = /[\w.+-]+@[\w-]+\.[a-zA-Z0-9.-]+/;
        const normalize = (value) =>
            typeof value === "string" ? value.trim().toLowerCase() : "";
        const authUser = String(
            args && args.auth_user !== undefined && args.auth_user !== null
                ? args.auth_user
                : "0"
        );

        // 1. 页面所属账号的官方 WIZ 字段（随 /u/N/ 页面一起下发）
        if (window.WIZ_global_data && typeof window.WIZ_global_data.oPEP7c === "string") {
            const wizEmail = normalize(window.WIZ_global_data.oPEP7c);
            if (EMAIL_RE.test(wizEmail)) return wizEmail;
        }
        if (document.documentElement && document.documentElement.innerHTML) {
            const mWiz = document.documentElement.innerHTML.match(/"oPEP7c"\s*:\s*"([^"@\s]+@[^"\s]+)"/);
            if (mWiz && mWiz[1]) return normalize(mWiz[1]);
        }

        // 2. 账号切换器里与当前 u/N 匹配的那一条：data-au 必须一致，
        //    否则会读到同浏览器里登录的另一个 Google 账号
        for (const el of document.querySelectorAll("[data-email][data-au]")) {
            if (String(el.getAttribute("data-au") || "") !== authUser) continue;
            const m = (el.getAttribute("data-email") || "").match(EMAIL_RE);
            if (m) return normalize(m[0]);
        }

        // 3. 其余来源（aria-label、"第一个像邮箱的字符串"等）都可能属于别的
        //    账号，宁可返回 null 交给 HTTP 探测，也不写入一个可能错误的身份。
    } catch (e) {}
    return null;
}
