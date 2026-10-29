# aistudio-api

<p align="center">
  <a href="https://github.com/DeconstructedCube/aistudio-api"><img src="https://img.shields.io/badge/API-Gemini%20v1beta-4285F4?style=flat-square&logo=google" alt="Gemini API"></a>
  <a href="https://hub.docker.com/r/bedrockblock/aistudio-api"><img src="https://img.shields.io/badge/Docker-bedrockblock%2Faistudio--api-2496ED?style=flat-square&logo=docker" alt="Docker Hub"></a>
  <a href="https://deepwiki.com/DeconstructedCube/aistudio-api"><img src="https://img.shields.io/badge/DeepWiki-DeconstructedCube%2Faistudio--api-5B68E4?style=flat-square&logo=data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAACwAAAAyCAYAAAAnWDnqAAAAAXNSR0IArs4c6QAAA05JREFUaEPtmUtyEzEQhtWTQyQLHNak2AB7ZnyXZMEjXMGeK/AIi+QuHrMnbChYY7MIh8g01fJoopFb0uhhEqqcbWTp06/uv1saEDv4O3n3dV60RfP947Mm9/SQc0ICFQgzfc4CYZoTPAswgSJCCUJUnAAoRHOAUOcATwbmVLWdGoH//PB8mnKqScAhsD0kYP3j/Yt5LPQe2KvcXmGvRHcDnpxfL2zOYJ1mFwrryWTz0advv1Ut4CJgf5uhDuDj5eUcAUoahrdY/56ebRWeraTjMt/00Sh3UDtjgHtQNHwcRGOC98BJEAEymycmYcWwOprTgcB6VZ5JK5TAJ+fXGLBm3FDAmn6oPPjR4rKCAoJCal2eAiQp2x0vxTPB3ALO2CRkwmDy5WohzBDwSEFKRwPbknEggCPB/imwrycgxX2NzoMCHhPkDwqYMr9tRcP5qNrMZHkVnOjRMWwLCcr8ohBVb1OMjxLwGCvjTikrsBOiA6fNyCrm8V1rP93iVPpwaE+gO0SsWmPiXB+jikdf6SizrT5qKasx5j8ABbHpFTx+vFXp9EnYQmLx02h1QTTrl6eDqxLnGjporxl3NL3agEvXdT0WmEost648sQOYAeJS9Q7bfUVoMGnjo4AZdUMQku50McDcMWcBPvr0SzbTAFDfvJqwLzgxwATnCgnp4wDl6Aa+Ax283gghmj+vj7feE2KBBRMW3FzOpLOADl0Isb5587h/U4gGvkt5v60Z1VLG8BhYjbzRwyQZemwAd6cCR5/XFWLYZRIMpX39AR0tjaGGiGzLVyhse5C9RKC6ai42ppWPKiBagOvaYk8lO7DajerabOZP46Lby5wKjw1HCRx7p9sVMOWGzb/vA1hwiWc6jm3MvQDTogQkiqIhJV0nBQBTU+3okKCFDy9WwferkHjtxib7t3xIUQtHxnIwtx4mpg26/HfwVNVDb4oI9RHmx5WGelRVlrtiw43zboCLaxv46AZeB3IlTkwouebTr1y2NjSpHz68WNFjHvupy3q8TFn3Hos2IAk4Ju5dCo8B3wP7VPr/FGaKiG+T+v+TQqIrOqMTL1VdWV1DdmcbO8KXBz6esmYWYKPwDL5b5FA1a0hwapHiom0r/cKaoqr+27/XcrS5UwSMbQAAAABJRU5ErkJggg==" alt="DeepWiki"></a>
  <a href="https://fastapi.tiangolo.com/"><img src="https://img.shields.io/badge/Framework-FastAPI-009688?style=flat-square&logo=fastapi" alt="FastAPI"></a>
  <a href="https://vuejs.org/"><img src="https://img.shields.io/badge/Frontend-Vue%203%20%2B%20Vite-4FC08D?style=flat-square&logo=vuedotjs" alt="Vue 3"></a>
  <a href="https://docs.astral.sh/uv/"><img src="https://img.shields.io/badge/Python-3.11%2B%20%7C%20uv-261230?style=flat-square&logo=python" alt="Python uv"></a>
  <a href="./LICENSE"><img src="https://img.shields.io/badge/License-MIT-green?style=flat-square" alt="License"></a>
</p>

<p align="center">
  Google AI Studio reverse proxy gateway that exposes native Google Gemini API interfaces.
</p>

<p align="center">
  <a href="./README.md">中文文档</a>
</p>

---

## Table of Contents

- [Features](#features)
- [Architecture & Documentation](#architecture--documentation)
- [Quick Start](#quick-start)
  - [Android (Termux)](#android-termux)
  - [Windows](#windows)
  - [Linux / macOS](#linux--macos)
  - [Docker Deployment](#docker-deployment)
- [Configuration & Environment Variables](#configuration--environment-variables)
- [API Usage Examples](#api-usage-examples)
  - [cURL](#curl)
  - [Python SDK (google-genai)](#python-sdk-google-genai)
  - [Function Calling](#function-calling)
- [Web Management Console](#web-management-console)
- [Browser Cookie Auto-Push (Userscript)](#browser-cookie-auto-push-userscript)
- [Development & Testing](#development--testing)
- [License](#license)

---

## Features

| Feature | Description |
|---|---|
| **Gemini API Compatibility** | Supports `/v1beta/...` endpoints including Thinking, multimodal inputs, function calling, and image generation |
| **OpenAI / Anthropic Compatibility** | Adds `/v1/chat/completions`, `/v1/messages`, `/v1/models`, and `/v1/messages/count_tokens` with streaming, tool calling, thinking, and image inputs; reuses the native Gemini pipeline instead of duplicating upstream logic (Beta) |
| **Dynamic Model Discovery** | Automatically synchronizes and discovers available models from upstream Google AI Studio |
| **Multi-Account Rotation** | Manages multiple accounts with automatic failover, tracks 429 rate limits per model independently, and resets quotas at midnight Pacific Time |
| **Runtime State Persistence** | Model usage statistics (`stats.json`) and account 429 cooldown & quota status (`rotator_state.json`) are automatically persisted to disk across restarts |
| **Cookie Writeback** | Every 60 seconds the active browser's latest cookies are written back to the account's `auth.json`; also saved before account switches and on shutdown to avoid restarting with a stale snapshot |
| **Instant Password Change** | The console's "Change Password" action verifies the current password and takes effect immediately without restarting; the new password is stored as a hash in `data/web_auth.json` and persists with the data volume |
| **Web Log Console** | New "Logs" page streams the latest 500 server log entries and lets you browse persisted request dumps; server logs and dump files can be cleared with one click, without logging into the server to run `docker logs` |
| **Account Health & Keepalive** | Proactively checks account cookies every 10 minutes; a 401 immediately marks the account expired and highlights it in the console. Every 30 minutes an idle AI Studio reload rotates cookies to reduce imported-session expiry |
| **Downstream Parameter Fault Tolerance** | `drop_unsupported_params` automatically drops unsupported safety categories (e.g. `HARM_CATEGORY_CIVIC_INTEGRITY`) and tools to prevent 400 errors |
| **Multi-Account Probing** | Extracts and verifies sub-accounts (`u/0`, `u/1`...) automatically when importing a multi-session Cookie |
| **Built-in Tools** | Supports official tools such as Google Search, Google Maps, and Code Execution sandbox |
| **Pure Python CDP** | Direct Chrome DevTools Protocol communication over asynchronous WebSockets without Node.js, Playwright, or Selenium |
| **Web Management Console** | Built-in web dashboard for account management (supporting text and JSON bundle file imports), live request metrics, and runtime `config.yaml` hot reloading |
| **Android Companion App** | Native Android app (`android/`) built with Jetpack Compose, featuring isolated multi-profile management, on-demand WebView, Bitwarden autofill, and one-click bundle export |
> [!NOTE]
> **Memory Footprint**: The standalone Python backend consumes ~35 - 45 MB RAM. With a single controlled Chromium instance, total actual system memory (PSS) is ~350 - 450 MB on Termux proot (~250 - 350 MB on native Linux). On Android Termux, ≥ 1 GB free RAM is recommended.

## Architecture & Documentation

```
┌────────────────────────────────────────────────────────┐
│               Client (cURL / Python SDK)               │
└───────────────────────────┬────────────────────────────┘
                            │ /v1beta/models/...
┌───────────────────────────▼────────────────────────────┐
│              FastAPI HTTP Gateway (Port 8080)          │
├───────────────────────────┬────────────────────────────┤
│    Account Rotator        │    Chat & Wire Codec       │
│  (Sticky & 429 Isolation) │ (Protobuf-JSON & BotGuard) │
└───────────────────────────┼────────────────────────────┘
                            │ CDP WebSocket
┌───────────────────────────▼────────────────────────────┐
│      Controlled Chromium Instance (CloakBrowser)       │
│       In-Page XHR Replay (withCredentials=true)        │
└───────────────────────────┬────────────────────────────┘
                            │ HTTPS / RPC
┌───────────────────────────▼────────────────────────────┐
│         Google AI Studio (alkalimakersuite-pa)         │
└────────────────────────────────────────────────────────┘
```

Technical reference documentation:

- [System Architecture Specification (ARCHITECTURE.md)](./docs/ARCHITECTURE.md): Layered architecture, request lifecycle, wire codec, and concurrency model.
- [Wire Protocol Reverse Engineering Specification (WIRE_SPECIFICATION.md)](./docs/WIRE_SPECIFICATION.md): Protobuf-JSON array mappings, field indices, multimodal Part structures, and Schema encoding.
- [BotGuard Verification Chain (BOTGUARD_VERIFICATION_CHAIN.md)](./docs/BOTGUARD_VERIFICATION_CHAIN.md): WAA challenge handshake, dynamic Wasm signing, content hashing, and upstream validation.
- [Agent & Engineering Conventions (AGENT.md)](./AGENT.md): Strict type specifications, testing standards, and development guidelines.

---

## Quick Start

### Android (Termux)

> [!TIP]
> **Download Notice**: Always install Termux from [F-Droid](https://f-droid.org/packages/com.termux/) or [GitHub Releases](https://github.com/termux/termux-app/releases). **Do NOT install from the Google Play Store** (the Play Store build is obsolete and will produce repository update errors).

Open Termux and run the following commands step by step:

```bash
# Step 1: Update Termux environment and install git
pkg update -y && pkg install -y git

# Step 2: Clone repository and enter directory
git clone https://github.com/DeconstructedCube/aistudio-api.git
cd aistudio-api

# Step 3: Initialize Python environment & sync dependencies (auto-resolves prebuilt Android wheels)
bash scripts/setup-env.sh
uv sync

# Step 4: Setup managed CloakBrowser runtime (first run only, takes ~1-2 min)
bash scripts/setup-browser.sh

# Step 5: Start server
uv run python main.py server --port 8080
```

Once started:
1. Open your phone's browser and go to **`http://localhost:8080`** to access the Web Console;
2. Navigate to **Accounts**, paste your Google account Cookies or import a bundle to start using the API;
3. **Background Persistence Tip**: Pull down Android's notification bar, tap `Acquire Wakelock` on Termux's notification, and disable battery optimization for Termux in phone system settings to prevent sleep kills.

---

### Windows

Prerequisite: [Python 3.11+](https://www.python.org/downloads/) installed (check `Add python.exe to PATH` during setup).

Open PowerShell or Windows Terminal:

```bat
# Step 1: Install uv package manager
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

# Step 2: Clone repository and enter directory
git clone https://github.com/DeconstructedCube/aistudio-api.git
cd aistudio-api

# Step 3: Sync dependencies
uv sync

# Step 4: Install CloakBrowser (first run only)
scripts\install-browser.bat

# Step 5: Start server
uv run python main.py server --port 8080
```

Open **`http://localhost:8080`** in your browser to access the Web Console.

---

### Linux / macOS

Open a terminal and run:

```bash
# Step 1: Install uv package manager
curl -LsSf https://astral.sh/uv/install.sh | sh

# Step 2: Clone repository and enter directory
git clone https://github.com/DeconstructedCube/aistudio-api.git
cd aistudio-api

# Step 3: Sync dependencies
uv sync

# Step 4: Install CloakBrowser (first run only)
bash scripts/setup-browser.sh

# Step 5: Start server
uv run python3 main.py server --port 8080
```

Open **`http://localhost:8080`** in your browser to access the Web Console.

---

### Docker Deployment

CloakBrowser is bundled in the container image for immediate use:

```bash
docker run -d \
  --name aistudio-api \
  --restart unless-stopped \
  -p 8080:8080 \
  -v aistudio-api-data:/app/data \
  ghcr.io/544r/aistudio-api:latest
```

Or run with Docker Compose:

```bash
docker compose up -d
```

Open **`http://localhost:8080`** in your browser to access the Web Console.

---

## Configuration & Environment Variables

Open `http://localhost:8080` in your browser to access the Web Console.

### Environment Variables
| Variable | Type | Default | Description |
|---|---|---|---|
| `AISTUDIO_PORT` | int | `8080` | HTTP service listening port |
| `AISTUDIO_WEB_PASSWORD` | string | `""` | Initial Web Console password (also checks `AISTUDIO_ADMIN_PASSWORD`); can be changed from the console after login |
| `AISTUDIO_WEB_AUTH_FILE` | string | `data/web_auth.json` | Persisted management-password hash; delete it and restart to fall back to `AISTUDIO_WEB_PASSWORD` |
| `AISTUDIO_PROXY` | string | `""` | Outbound proxy URL (`http://`, `https://`, or `socks5://`) |
| `AISTUDIO_DATA_DIR` | string | `data/` | Main data directory for persistent files (`/app/data` in Docker) |
| `AISTUDIO_ACCOUNTS_DIR` | string | `data/accounts` | Directory for storing account credentials |
| `AISTUDIO_STATS_FILE` | string | `data/stats.json` | Persistent storage file for model usage metrics |
| `AISTUDIO_ROTATOR_STATE_FILE` | string | `data/rotator_state.json` | Persistent storage file for account 429 cooldowns and dispatch states |
| `AISTUDIO_CONFIG_FILE` | string | `config.yaml` | Path to model rules and default tools configuration YAML |
| `AISTUDIO_PERSIST_STATS` | bool | `true` | Persist request and token usage metrics across service restarts |
| `AISTUDIO_PERSIST_ROTATOR` | bool | `true` | Persist account 429 rate limit dates & cooldowns across restarts |
| `AISTUDIO_BROWSER_EXECUTABLE` | string | Auto | Path to CloakBrowser executable (`/opt/cloakbrowser/chrome` in Docker) |
| `AISTUDIO_BROWSER_PORT` | int | `9222` | Chromium remote debugging port (CDP) |
| `AISTUDIO_BROWSER_HEADLESS` | bool | `true` | Run Chromium in headless mode |
| `AISTUDIO_PROOT_NAME` | string | `aistudio-api` | Dedicated proot-distro container name for Termux |
| `AISTUDIO_DEFAULT_TEXT_MODEL` | string | `gemini-3.7-flash` | Default text model |
| `AISTUDIO_DEFAULT_IMAGE_MODEL` | string | `gemini-3.1-flash-image-preview` | Default image generation model |
| `AISTUDIO_TIMEZONE` | string | Auto | Browser fingerprint & Wire protocol timezone (`America/Los_Angeles` by default) |
| `AISTUDIO_LOCALE` | string | `en-US` | Browser fingerprint locale |
| `AISTUDIO_DUMP_REQUESTS` | bool | `false` | Dump API request/response exchanges to console logs (enabled via `DEBUG=1`) |
| `AISTUDIO_DUMP_FILE` | bool | `false` | Dump request/response exchanges to individual disk files |
| `AISTUDIO_DUMP_DIR` | string | `dumps/` | Target directory for dumped exchange files |
| `AISTUDIO_ACCOUNT_HEALTH_INTERVAL` | int | `600` | Seconds between proactive account session checks; `0` disables. A 401 from `ListModels` marks the account expired |
| `AISTUDIO_SESSION_KEEPALIVE_INTERVAL` | int | `1800` | Seconds between idle session keepalives; reloads AI Studio to rotate cookies. `0` disables |
| `AISTUDIO_BLOCKED_EMAILS` | string | empty | Blocked emails (comma-separated). Accounts with these emails are never imported/pushed and are disabled at startup; use it to exclude your primary account from a multi-account cookie jar |

> [!IMPORTANT]
> Cookie-imported accounts have no OAuth refresh token. If Google has already revoked the session (password change, risk control, device session cleanup, etc.), no keepalive can renew it server-side; re-importing cookies is the only recovery. Health checks and keepalives reduce expiry probability and make expired accounts visible in the console before an API call fails.

---

## API Usage Examples

Supported authentication methods:
- Query param: `?key=YOUR_API_KEY`
- Header: `x-goog-api-key: YOUR_API_KEY`, `x-api-key: YOUR_API_KEY`, or `Authorization: Bearer YOUR_API_KEY`

### cURL

<details>
<summary><b>1. List Models</b></summary>

```bash
curl http://localhost:8080/v1beta/models \
  -H "x-goog-api-key: your-api-key"
```
</details>

<details>
<summary><b>2. Generate Content (Non-streaming)</b></summary>

```bash
curl http://localhost:8080/v1beta/models/gemini-3.7-flash:generateContent \
  -H "x-goog-api-key: your-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "contents": [
      {
        "role": "user",
        "parts": [{"text": "Explain quantum computing in one sentence."}]
      }
    ]
  }'
```
</details>

<details>
<summary><b>3. Stream Generate Content (SSE)</b></summary>

```bash
curl http://localhost:8080/v1beta/models/gemini-3.7-flash:streamGenerateContent?alt=sse \
  -H "x-goog-api-key: your-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "contents": [
      {
        "role": "user",
        "parts": [{"text": "Explain how Python asyncio works."}]
      }
    ]
  }'
```
</details>

---

### Python SDK (google-genai)

Using the official `google-genai` Python SDK:

```python
from google import genai

client = genai.Client(
    api_key="your-api-key",
    http_options={
        "api_version": "v1beta",
        "base_url": "http://localhost:8080",
    },
)

# 1. Streaming response
response = client.models.generate_content_stream(
    model="gemini-3.7-flash",
    contents="What is reactive programming?",
)
for chunk in response:
    print(chunk.text, end="", flush=True)

# 2. Non-streaming response
result = client.models.generate_content(
    model="gemini-3.7-flash",
    contents="Write a quicksort implementation in Python.",
)
print(result.text)
```

---

### Function Calling

```python
from google import genai
from google.genai import types

client = genai.Client(
    api_key="your-api-key",
    http_options={
        "api_version": "v1beta",
        "base_url": "http://localhost:8080",
    },
)

# Tool declaration
weather_tool = types.Tool(
    function_declarations=[
        types.FunctionDeclaration(
            name="get_current_weather",
            description="Get real-time weather conditions for a given location",
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "location": types.Schema(
                        type="STRING",
                        description="City name, e.g. 'San Francisco' or 'Tokyo'",
                    ),
                    "unit": types.Schema(
                        type="STRING",
                        enum=["celsius", "fahrenheit"],
                        description="Temperature unit",
                    ),
                },
                required=["location"],
            ),
        )
    ]
)

response = client.models.generate_content(
    model="gemini-3.7-flash",
    contents="What is the weather in Tokyo right now?",
    config=types.GenerateContentConfig(tools=[weather_tool]),
)

if response.function_calls:
    for call in response.function_calls:
        print(f"Tool called: {call.name}")
        print(f"Arguments: {call.args}")
```

---

## Web Management Console

The service includes a built-in static Web Console served at `http://localhost:8080`:

- **Dashboard**: Real-time view of service health, per-model request stats, success rates, and rate-limit cooldown status.
- **Accounts**: Import Cookie credentials (single text or bundle files), auto-probe multiple accounts, switch the active account, or reset rate limits; also offers a "Download userscript" button (served at `/userscripts/aistudio-cookie-push.user.js` without authentication).
- **Settings**: Online editor for `config.yaml` with instant hot-reloading on save.
- **Logs**: Live view of the latest 500 server log entries with pause, refresh, and one-click clear; lists, opens, deletes, or clears all request-dump JSON files under `dumps/`. Health checks and the log endpoints' own polling are excluded to keep the view readable.
- **Access Control**: Enable authentication by setting `AISTUDIO_WEB_PASSWORD`. After logging in, use "Change Password" in the top bar to verify the current password and update it instantly without restarting. The new password is stored as a hash in `data/web_auth.json` and persists with the `data` volume; delete that file and restart to fall back to the environment password.

---

## Browser Cookie Auto-Push (Userscript)

Imported cookies are not permanent: Google rotates sessions and revokes stale ones over time, so a pasted cookie is only a snapshot. If your daily Chrome stays signed in to the same Google accounts, let `userscripts/aistudio-cookie-push.user.js` push fresh cookies to this service on a schedule. The server rewrites the matching account's `auth.json`, clears the "session expired" flag, and hot-injects the new cookies into the running Camoufox for the active account — no restart and no manual re-paste.

Setup:

1. Click "Download userscript" on the console's Accounts page (or open `/userscripts/aistudio-cookie-push.user.js` on your server) and install it in your userscript manager; you can also create a new script and paste the repository's `userscripts/aistudio-cookie-push.user.js`.
2. Set "Allow scripts to access cookies" to **all** in Tampermonkey (Settings → Advanced config mode → Security → All → Save); otherwise HttpOnly session cookies cannot be read.
3. Open any AI Studio page and use the script menu:
   - "⚙ Set server address": e.g. `http://192.168.1.10:8000`
   - "🔑 Set admin password": the `AISTUDIO_WEB_PASSWORD` value (leave empty when auth is disabled)
   - "⏱ Set push interval": 60 minutes by default
4. On the first push the script manager asks to connect to your server — choose "Always allow".
5. The script pushes once after page load, then on every interval. A status pill in the bottom-right corner shows the last result; click it to push immediately.
6. Two optional guards for long-running setups (menu toggles, off by default): "🔊 Silent audio keepalive" loops an inaudible track so the tab is not frozen/discarded (which would stop the pushes), and "🔄 Set auto-refresh interval" reloads the page every N minutes while the tab is hidden, forcing fresh requests and cookie rotation, then pushes again. The keepalive toggle reports the real playback state in a dialog (if autoplay policy blocks it, the script asks you to click the page once), and the status pill shows a 🔊 marker while it is active; the menu entry also reflects the current state.
7. To update the script, use "🔄 Check for updates": it compares the `@version` served by your server and GitHub, then opens the install page when a newer build exists (account and server settings are preserved), so no manual re-download is needed.
8. The "Cookie push" window on the Logs page records every push accepted by the server: time, account, session state, whether it was hot-injected, the pushed-jar fingerprint, the merged fingerprint, and the diff against the previous push for the same account (core identity cookies / rolling tickets / other). Only cookie names and short value fingerprints are shown; plaintext values are never displayed. A 15-minute push that only rotates `__Secure-*PSIDTS` is labelled "rolling ticket change", while changes to `SID`, `SAPISID`, etc. are labelled "core cookie change". The history is kept in memory and cleared on restart.

Server behavior (`POST /api/accounts/push-cookies`):

- Matches the account by `email` (or an explicit `account_id`); creates a new account when no match exists.
- Accounts sharing the same `cookie_id` group (one cookie jar imported by paste) are refreshed together.
- A successful push clears the account's "session expired" flag and immediately probes the new cookies with `ListModels`; a failed probe marks it expired again.
- If the account is currently `Free`, the push also probes Google's benefit-tier endpoint with the new cookies and upgrades Pro / Ultra / Plus automatically, so no manual "Detect tier" click is needed; accounts already on a paid tier skip the extra probe.
- When the pushed account is active and the browser is online, the new cookies are hot-injected into the running browser so a later write-back cannot overwrite them with the old jar.
- Only `SID` and `SAPISID` (the signing cookie) are mandatory. `__Secure-*PSID` may be invisible to extensions in Chrome's partitioned-cookie scenario; the push is then merged with the account's existing `auth.json` (new values win, old cookies are kept) and `ListModels` makes the final call.

> Note: the payload contains full Google session cookies. Only point the script at a trusted server, and prefer a dedicated test account.

### Multi-Account Browsers and Primary-Account Protection

One cookie jar can be signed in to several Google accounts (`/u/0/`, `/u/1/`, …); the server distinguishes them by the `auth_user` index. If the browser also has a **primary account you never want to use**, either of these two layers is enough to guarantee it is not used:

1. **Server-side blocklist (hard guarantee)**: put the primary account's email in `AISTUDIO_BLOCKED_EMAILS` (comma-separated). Imports, probe-imports, bundle imports, and userscript pushes then reject/skip that email, and any existing account with that email is disabled automatically at startup (the rotator only selects enabled accounts).
2. **Userscript allowlist (source-side guard)**: in the script menu choose "✅ Set allowed accounts" and fill in the emails allowed to push. If the current page is on the primary account (typically the default `/u/0/` page), automatic pushes are skipped and the status pill says so; if the email cannot be detected the push is also skipped rather than guessed.

The two layers are independent: the server blocklist protects against any client, while the script allowlist avoids useless pushes and reduces how often cookies leave the browser.

---

## Development & Testing

Frontend code is located in `web/` (Vue 3 + TypeScript + Vite + Tailwind CSS):

```bash
cd web
bun install
bun run dev          # Start local Vite dev server (localhost:3000 -> 8080)
bun run type-check   # Run TypeScript type check
bun run lint         # Run ESLint check
bun run build        # Build production assets into src/aistudio_api/static
```

Python quality checks and tests:

```bash
# Run code style & lint check
uv run ruff check .
# Run static type checking
bun x pyright

# Run test suite
uv run pytest
```

Android companion app code style checks:

```bash
# Run Kotlin code style check (configured via ~/.zshrc)
ktlint "android/app/src/main/java/**/*.kt"
```

---

## License

This project is licensed under the [MIT License](./LICENSE).
