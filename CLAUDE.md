# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

Two components for **Microsoft Family Safety** in Home Assistant, shipped from one repo:

- `custom_components/microsoft_family_safety/` — the HACS **integration** (Python, HA-side). Domain `microsoft_family_safety`. **This is the whole product in the normal case**: it authenticates natively and talks to both Microsoft APIs itself.
- `familysafety-playwright/` — the **legacy auth add-on**: a FastAPI service that drives a Chromium (Playwright) to hold an authenticated Microsoft browser session. Shipped both as an HA OS add-on and as a standalone Docker image. Since native authentication landed it is a **fallback**, kept for existing installs and for cases where the native flow cannot run.

They are versioned and released together (integration `manifest.json` and add-on `config.json` must be at the same version; bump both when cutting a release). There is **no test suite** and **no linter config** in the repo; verification is manual against a real Microsoft Family Safety account.

## The central architecture fact: two Microsoft APIs, two auth schemes

This is the single most important thing to understand before changing anything. Microsoft Family Safety has two backends, and capabilities are split across them:

| API | Host | Auth | What it does | Code |
|-----|------|------|--------------|------|
| **Mobile API** | `mobileaggregator.family.microsoft.com` | OAuth Bearer (`MSAuth1.0` token from a refresh token) | Roster, app list, screen-time *usage*, web restrictions, content restrictions; block/unblock apps | `api_client.py` (`_request`/`_build_headers`), `pyfamilysafety` lib |
| **Web API** (private) | `account.microsoft.com/family/api/` | Microsoft session **cookies + `__RequestVerificationToken`** | Screen-time **schedule** read/write (daily allowance, time intervals), app time limits, web filtering, age rating, ask-to-buy | `api_client.py` `FamilySafetyWebAPI._web_request`, over httpx |

**Screen-time schedule writes only work through the Web API.** The mobile API's schedule/device-override endpoints are **broken** (Microsoft removed/changed them — they return 400); do not route schedule changes through them.

### What changed with native auth — read this before trusting older notes

The old rule "the Web API only works from inside a real Playwright browser" is **no longer true at runtime**. Once a valid Family context exists (session cookies + a Family-SPA `__RequestVerificationToken`), Home Assistant calls `account.microsoft.com/family/api/*` **directly over httpx**, in-process, with no add-on and no browser. A screen-time write is now a plain HTTP POST, not a 15-30s browser navigation. A lock is still 14 POSTs (7 days × allowance+intervals), but they are cheap.

What still needs a browser is **acquiring** the Family context, and only in the fallback case. Authentication has two phases:

The order is **web first, mobile second** (since 2.0.4). It used to be the reverse, and that reversal is why sessions died after about 7 h: the mobile OAuth page (`oauth20_authorize.srf`, client `000000000004893A`, `lw=1`) never shows "Stay signed in?", so `MSPAuth`/`MSPProf`/`WLSSC` were session cookies that Microsoft invalidated server-side within hours. The web sign-in on `account.microsoft.com` does show it, which is what gave the Playwright add-on weeks of validity.

- **Phase 1 — Family web session (browser).** `config_flow.async_step_start_mobile_auth` (the step id kept its historical name) starts the proxy in `completion_mode="web"` on `https://account.microsoft.com/` with no preloaded cookies. The user signs in through the temporary reverse proxy mounted inside HA's own HTTP server (`auth/native_proxy.py`) and must answer **Yes** to "Stay signed in?". The proxy then walks `_next_family_bootstrap_target()` (three landing routes) in that same tab, scrapes `__RequestVerificationToken`, and completes; the flow passes through `wait_family_proxy` → `finish_proxy`.
- **Phase 2 — mobile refresh token (server-side).** `finish_proxy` sees `_pending_data is None` (web-first marker) and calls `_finish_web_first` → `_try_server_side_mobile_oauth` → `native_proxy.async_fetch_mobile_oauth_redirect`: with the fresh SSO cookies, `oauth20_authorize.srf` answers with a single 302 to `oauth20_desktop.srf?code=...` (verified against a real account; no sign-in page, no consent). `validate_redirect_url` exchanges the code and `_apply_mobile_oauth_info` builds `_pending_data` (including the `wrong_account` check on reauth). If Microsoft ever demands an interactive step, the fallback preloads the web cookies into an `oauth`-mode proxy on the same external step (`_web_first_capture` keeps the web session aside; `async_step_start_web_auth` then merges it and shows the success form).

Legacy add-on entries (`_uses_legacy_addon()`) keep the old mobile-only journey (`oauth` proxy → `finish_mobile_proxy`); their web session lives in the add-on. The old phase-B pieces (`_try_server_side_family_bootstrap`, browser bootstrap with `initial_cookies`) still exist for that path and for the fallback, and the browser part is still **necessary**: a cold-start Family bootstrap with no SSO cookies cannot work, because Microsoft gates the Family SPA behind an interactive `prompt=none` OAuth hop (`login.microsoftonline.com/common/oauth2/v2.0/authorize`). At runtime the same condition surfaces as `api_client.py` `"Microsoft Family context requires browser authentication"` with `family_context_state = "auth_required"` and `last_web_error_code = "FAMILY_CONTEXT_AUTH_REQUIRED"`. Do not "simplify" the web phase into a pure-httpx path.

The Playwright add-on (`familysafety-playwright/`) is now a **legacy fallback**, selected only when the config entry has no `web_cookies`. See "Native vs legacy mode" below.

### Transport patches (applied in `__init__.async_setup_entry`)

- **`_httpx_web_adapter.py`** — `apply_httpx_web_transport_patch(hass)` replaces `FamilySafetyWebAPI._get_web_session` with an aiohttp-shaped façade (`_HttpxSessionAdapter`, `_CookieJarAdapter`, `_ResponseAdapter`, `_RequestContext`) over `create_async_httpx_client(hass, ...)`. `api_client._web_request` is unchanged; httpx errors are re-raised as `asyncio.TimeoutError` / `aiohttp.ClientError` so the existing except-clauses still match. It shares marker `_hafs_ipv4_threaded_web_transport_patch` with the older aiohttp IPv4/`ThreadedResolver` patch in `_pyfamilysafety_compat.py`, which therefore becomes a no-op.
- **`_httpx_web_tuning.py`** — `apply_httpx_web_tuning_patch()` (a) forces `httpx.Timeout(connect=30, read=120, write=30, pool=30)` and injects HAR-derived browser client hints (`Sec-Fetch-*`, `sec-ch-ua*`, `X-Edge-Shopping-Flag`, plus `Correlation-Context` only on `/family/api/st`) on `account.microsoft.com` requests; (b) wraps `async_check_web_session` so the extra `/account` probe is skipped when the Family context is already `ready`.

Both are monkey-patches, and so is `_pyfamilysafety_compat.apply_patches` (called from `coordinator._async_setup_api`): **reading `api_client.py` alone gives the wrong answer** for screen-time reads, web-browsing reads and the session probe. In particular the patches *invert* two documented preferences — patched `get_screentime_policy` tries `/family/api/st` **first** (source `st_har`, with a 30-min backoff on timeout/network error) and only then the original `landing-page-feeds` path; `get_web_browsing_settings` becomes **mobile-first**. The in-file docstrings still describe the pre-patch order; trust the patch.

## Native vs legacy mode

One boolean decides everything: `coordinator._native_web_auth = bool(entry.data.get(CONF_WEB_COOKIES))` (re-evaluated after the runtime auth store loads).

| | native (default) | legacy add-on |
|---|---|---|
| cookies from | `entry.data[CONF_WEB_COOKIES]` / runtime auth store | `addon_client.load_cookies()` |
| screen-time read | `web_api.get_screentime_policy()` (httpx) | `addon_client.fetch_screentime()`, httpx as fallback |
| screen-time write | `web_api.set_screentime_*` (httpx) | `addon_client.set_screentime_*` |
| expiry signal | `web_api.web_session_state == "expired"` | `addon_client.last_error_code == "LOGIN_REDIRECT"` |

`AddonCookieClient` is still constructed unconditionally in `coordinator.__init__`, but in native mode it is **dead code at runtime — not a per-request fallback**. There is no automatic native→add-on downgrade: if native cookies go missing, the coordinator requests reauth instead.

Credentials live in the config entry (`CONF_WEB_COOKIES`, `CONF_WEB_FAMILY_TOKEN`, `CONF_WEB_FAMILY_REFERER`, `CONF_REFRESH_TOKEN`) **in clear text**, plus a per-entry runtime `Store` (`microsoft_family_safety.auth.{entry_id}`) that absorbs rotated cookies/tokens on each poll without rewriting the entry (anchored to a SHA-256 of the entry credentials, so a reconfigure discards the stale cache). This is a genuine regression versus the add-on's Fernet-encrypted `cookies.enc` — keep it documented, do not paper over it.

The web session renews itself **as long as the Microsoft-account cookies are alive**, and only then. `account.microsoft.com` keeps its own session (`AMCSecAuth`) for about 24 h; when it lapses the site bounces to `login.live.com/login.srf`, which with valid MSA cookies is not a sign-in page but a 2 KB "Continue" document carrying an auto-submit `fmHF` form to `/auth/complete-signin`. `api_client._async_renew_account_session()` (2.0.6) posts that form exactly like a browser would; Microsoft then chains `complete-signin` → `login.microsoftonline.com/consumers/oauth2/v2.0/authorize` → `login.live.com/oauth20_authorize.srf` → `complete-signin-oauth` with plain 302s and the Family page is back with its token. Both `_warm_web_session()` and `_warm_family_context()` try it once before reporting `expired` / `auth_required`. `_extract_msa_continue_form()` refuses any page with `loginfmt`/`PPFT`/`passwd` fields or a form posting elsewhere, so a real sign-in page still ends in reauth. Before 2.0.6 that landing page was classified as a login redirect, which is why every install had to re-authenticate every 24 h. `sync_web_cookies_from_session()` captures Microsoft's `Set-Cookie` rotations on every 2xx/3xx, and `_warm_family_context()` re-scrapes the antiforgery token whenever it is empty; a genuinely expired login can only be fixed by re-running the flow. `_request_reauth(reason)` calls `entry.async_start_reauth()` (HA de-duplicates) and a single persistent notification `familysafety_auth_expired` is raised.

Microsoft drops the Family session **hours before** `/account` stops answering 200 (observed: `401` on `/family/api/st` about 7 h after sign-in, with the SSO cookies captured as session cookies). From then on `_warm_family_context()` lands on `login.microsoftonline.com/.../authorize` on every poll (`family_context_state = "auth_required"`) and nothing server-side can rebuild the context. `coordinator._async_track_family_context()` counts consecutive polls in that state and, after `_FAMILY_AUTH_REQUIRED_POLLS` (2), raises the notification and `_request_reauth("family_context_auth_required")`; the `/account` probe deliberately does not clear that reauth (`_family_context_dead_end`), since `/account` still succeeds in exactly that state. Before this existed the entry sat on "connected" with every schedule `unknown` and no prompt. `api_client` also memoises the login-page verdict for `_FAMILY_AUTH_REQUIRED_MEMO_SECONDS` (one bootstrap per poll instead of one per caller) and logs the WARNING only on the transition.

## The native auth proxy (`auth/native_proxy.py`)

- Two `HomeAssistantView`s, both **`requires_auth = False`**: `MicrosoftFamilyAuthorizationProxyView` at `/auth/microsoft_family_safety/proxy/{token}` (plus `{tail:.*}`, all verbs on one handler) and `MicrosoftFamilyAuthorizationCallbackView` at `/auth/microsoft_family_safety/callback`. The only authorization is the unguessable per-flow `secrets.token_urlsafe(24)` in the path; `register_native_proxy` expires the flow after **600 s** and `async_close()`s the client (afterwards the route returns `HTTPGone`).
- Upstream targets are restricted by `_is_allowed_host` to `live.com`, `microsoft.com`, `microsoftonline.com` (exact or dotted-suffix match). An explicit port is stripped before the check (`login.microsoftonline.com:443`, which Microsoft's MSAL sign-in page emits, used to be rejected with "Microsoft authentication host not allowed"; the proxy route strips it too since 2.0.4). Never widen the host list — the view is unauthenticated, and widening turns it into an open proxy.
- `_extract_family_request_verification_token` accepts **only** `__RequestVerificationToken`. Do not fall back to `apiCanary`/`canary`: those belong to a different antiforgery context and yield 401.
- `_has_authenticated_cookie_set()` requires ≥2 of `MSPAuth`, `MSPProf`, `WLSSC`, `RPSAuth`, `RPSSecAuth`. `export_cookies()` exports only `microsoft.com`/`live.com` cookies, in the Playwright-compatible shape.
- **`_install_security_filter_bypass`** — HA's `homeassistant/components/http/security_filter.py` middleware rejects any query string matching `[a-zA-Z0-9_]=/([a-z0-9_.]//?)+`. Microsoft's silent-SSO redirects carry `epctrc=/w/...`, which matches, so the browser gets a bare `400 Bad Request` before the view ever runs. Percent-encoding does not help — `request.query_string` is already decoded. The 400 is **intermittent**: it depends on which OAuth path Microsoft picks for a given sign-in.
  **Do not implement this by touching `app.middlewares`.** aiohttp freezes that list when the HTTP server starts, and this code runs from the config flow long afterwards, so an insert always raises `RuntimeError: Cannot modify frozen list` and breaks *every* setup, native and legacy alike (issues #39/#40/#41, shipped broken in 2.0.0, fixed in 2.0.1). Instead the bypass wraps the module-level `security_filter.FILTERS` regex with `_ScopedSecurityFilter`: `search()` returns no match for a path under `AUTH_PROXY_PATH`, and — via the `_IN_PROXY_REQUEST` ContextVar set during that path scan — for the query-string scan of that same request. HA's middleware calls `FILTERS.search(path)` then `FILTERS.search(query_string)` in one coroutine, and aiohttp runs each request in its own task, so the marker cannot leak between requests. Everything else keeps the original filter; the callback route is not exempted.
- HTTPS enforcement is **not** here — it lives in `config_flow._browser_hass_url()`.

## Integration data flow

```
HA service call / entity write
   └─ coordinator method (async_*)
        ├─ pyfamilysafety          → mobile API (token)   [app block, approve/deny request]
        ├─ api_client._request     → mobile API (token)   [web filter read, content restrictions]
        └─ api_client._web_request (httpx, native mode)   [screen-time schedule, app limits,
             → account.microsoft.com/family/api/*          websites, age rating, ask-to-buy]
             (legacy mode only: addon_client → add-on browser fetch())
   └─ async_request_refresh()

Poll loop: coordinator._async_update_data()
   ├─ _async_setup_api()   (first run: apply_patches, FamilySafety.create, build web_api)
   ├─ _async_load_web_cookies()  → set_web_cookies(cookies, family_token, family_referer)
   ├─ web_api.async_check_web_session()   (native mode; skipped when Family context is ready)
   ├─ api.update()  (pyfamilysafety, mobile API)  → roster, devices, usage, apps
   ├─ per account: _fetch_web_api_data()
   │     ├─ web_browsing      (mobile-first after patching, web as fallback)
   │     ├─ content_settings  (mobile only)
   │     └─ _fetch_screentime_policy()  → httpx /family/api/st, then landing-page-feeds
   └─ finally: _async_persist_current_auth()   (rotated refresh_token + cookies + Family token)
```

- **`coordinator.py`** is the hub: `FamilySafetyDataUpdateCoordinator` owns three clients (`self.api` = pyfamilysafety, `self.web_api` = `FamilySafetyWebAPI`, `self._addon_client` = `AddonCookieClient`) and exposes one `async_*` method per service. All entity reads come from `coordinator.data` (`{"accounts": {...}, "devices": {...}, "pending_requests": [...]}`). `_fetch_web_api_data` carries forward the previous poll's `web_browsing`/`content_settings`/`screentime_policy` when a fetch returns `None`, so a transient web failure does not blank the entities — never applied to measured usage.
- `FamilySafetyWebAPI` is constructed with **the pyfamilysafety authenticator itself**, so there is exactly one refresh-token chain. Do not create a second one: two independent grants would rotate the same Microsoft refresh token out from under each other.
- **`__init__.py`** registers the 18 services via a table-driven loop (`_register_services`): each row is `(name, schema, coordinator_method, args_extractor)`. To add a service: add the const + schema + a coordinator method + one table row. It also applies the two httpx patches before building the coordinator.
- Entities live in `sensor.py`, `switch.py`, `button.py`, `number.py`, `time.py`. **All five platforms add entities dynamically**: `async_setup_entry` runs an `_add_new_entities()` closure that diffs against a `known_*` set, then re-registers it via `coordinator.async_add_listener(...)`. New children/apps/devices appear without reload. Match this pattern when adding entities.
- HA devices: one per child account (`Microsoft / Family Safety Account`) and one per physical device, linked with `via_device`. Plus one integration-level `FamilySafetyConnectionSensor` (`{entry_id}_connection`, diagnostic category) exposing `connection_state()`.

## Config flow (native)

`user` → `start_mobile_auth` (historical name; for native entries it starts the **web** proxy, external step opening `/auth/microsoft_family_safety/proxy/{token}`) → `check_mobile_proxy` (shared external step id, deliberate: the user must not press "Open website" twice) → `wait_family_proxy` (`async_show_progress`, `progress_action="family_sso"`) → `finish_proxy` → `_finish_web_first` (server-side mobile OAuth; browser `oauth` proxy on the same step id as fallback) → `auth_success` / `reauth_success`. Legacy add-on entries: `start_mobile_auth` (`oauth` proxy) → `check_mobile_proxy` → `finish_mobile_proxy` → entry created/updated without a web phase. A `microsoft_family_safety.request_reauth` service (`coordinator.async_request_reauth`) starts the reauth flow on demand, e.g. from a dashboard.

- `_browser_hass_url()` prefers an **HTTPS** HA URL. With no HTTPS URL it raises `NativeAuthHttpsRequired`; the opt-in `allow_insecure_http_auth` (`CONF_ALLOW_INSECURE_HTTP_AUTH`) permits a plain-HTTP URL **only** when `_is_local_http_url` accepts it (`localhost`, `homeassistant`, `*.local`, or a private/loopback/link-local IP) and logs a WARNING that Microsoft credentials are unencrypted on the LAN. Otherwise `NativeAuthLocalHttpRequired`.
- The manual "paste the redirect URL" step (`async_step_auth`) survives but is reachable from **exactly one** branch: `NativeAuthHttpsRequired` **and** a legacy add-on was detected or configured (`_detected_source in ("api", "file") or _pending_auth_url`). A plain non-add-on install without HTTPS gets error `native_https_required` instead — it is not offered a manual path, because a native entry is only complete when both phases succeed.
- Reauth and reconfigure re-run the whole native journey and renew **both** the mobile refresh token and the Family web session. `reauth_success` deliberately `setdefault`s `CONF_WEB_FAMILY_TOKEN`/`CONF_WEB_FAMILY_REFERER` to `None` so a stale antiforgery token cannot survive a failed capture. A user-id mismatch aborts with `wrong_account`.
- Known string gap: `native_https_required` / `native_http_not_local` are used as `async_abort` reasons in reauth but exist only under `config.error`, not `config.abort`, so reauth on an HTTP-only instance shows the raw key.

## Account lock & policy toggle (the subtle part)

There is no working "lock account" Microsoft endpoint. **Lock = set all 7 days' allowance to 0**; unlock = restore the saved schedule. Saved schedules are persisted to HA `Store` (`microsoft_family_safety.saved_screentime`) so lock/unlock survives restarts (`async_load_saved_screentime` runs in `async_setup_entry`).

Critical guard in `async_lock_account`: it **refuses to zero the schedule** if it cannot read the current one AND has no saved restore point (`raise UpdateFailed`, before any write) — otherwise a child's real schedule would be wiped unrecoverably (issue #23). A second half of the guard only overwrites the saved policy when the freshly read one has a non-zero allowance (`has_nonzero`), so locking twice cannot save an all-zero schedule over a good one. Preserve both. This survived the move to native auth unchanged: the lock still reads the current schedule first, and `has_saved_policy` (computed in `switch.py` as `account_id in coordinator._saved_screentime`) flips to `true` before the zeroing POSTs.

On partial failure, lock/unlock/disable count failures, `raise UpdateFailed` and **retain** the saved policy so the operation can be retried; `_saved_screentime.pop()` only happens on a fully successful unlock. Both loops also **stop at the first network-level failure** (`_is_transient_web_failure()`): each weekday is two web writes that can each wait out the 120 s read timeout, so continuing would block the coordinator for minutes without writing anything.

Three ways this restore path lost a child's real schedule, all fixed in 2.0.3 — keep them in mind before touching it:
- **Key types.** Account ids arrive as `int` from the mobile-API roster and as `str` everywhere else, and JSON object keys are always `str`. So a restore point saved before a reload became invisible afterwards (`1055519684390826 in {"1055519684390826": ...}` is False), `has_saved_policy` flipped to false, and unlock overwrote the schedule with its default. Always go through `_policy_key()` (and `str(...)` in `switch.py`).
- **Saving.** `_async_save_screentime` merges onto what is on disk instead of writing the in-memory dict: a reload builds a fresh coordinator whose dict starts empty, and a save before `async_load_saved_screentime` had run would wipe the file. Only accounts in `_released_screentime` (released after a fully successful unlock) are removed.
- **Unlock with no restore point** raises `UpdateFailed` instead of writing the 2h/day, 07:00-22:00 default. Inventing a schedule is the same destructive write the lock guard refuses.

`is_account_locked` = all 7 days at `00:00:00`. The "screen-time limits on/off" switch (`async_set_policy_enabled`, issue #24) reuses the same save/restore machinery *and the same guard*: off = all days to 24h + all 48 slots true, on = restore via `async_unlock_account`.

## The add-on (`familysafety-playwright/`) — legacy fallback

Only reached when `_native_web_auth` is False (no `web_cookies` in the entry). Still worth understanding because existing entries run on it and it must keep working.

- **`app/main.py`** — FastAPI. Endpoints under `/api/*`. Two auth tiers:
  - `_verify_api_key` (`X-API-Key`) guards the high-harm endpoints: `/api/cookies`, `/api/screentime*`. The key is auto-generated and written to `/share/familysafety/.api_key` (mode 0600) so the integration on the same host reads it transparently; set `API_KEY` env / add-on option only when HA is on another host.
  - `_verify_ui_token` (`X-UI-Token`) guards the browser-driven `/api/auth/start|status` — a weak per-process token injected into the served HTML so only the page can start auth.
- **`app/auth/browser.py`** — `BrowserAuthManager`, the Playwright core:
  - One Chrome **persistent context** on profile `/share/familysafety/browser_profile` (survives restarts). A single `asyncio.Lock` (`_browser_lock`) serializes **everything** — auth and all API calls. `MAX_CONCURRENT_SESSIONS = 1`. If auth holds the lock, `browser_fetch` returns `BROWSER_BUSY` (503) immediately rather than blocking the HA request.
  - The shared API context is kept warm and closed after `CONTEXT_IDLE_TIMEOUT` (180s) of inactivity, so bursts reuse it.
  - `_wait_for_family_dashboard` handles Microsoft's OAuth-silent intermediate redirects; auth-like failures from a reused context trigger one recycle-and-retry.
  - The `canary` cookie is httpOnly, so it's read on the Python side and passed into the page `fetch()`; the actual CSRF header value comes from the `__RequestVerificationToken` DOM input.
- **`app/storage/file_storage.py`** — cookies are Fernet-encrypted at `/share/familysafety/cookies.enc` with key `/share/familysafety/.key`. The integration's `AddonCookieClient` can decrypt this file directly as a fallback when the add-on HTTP API is unreachable.
- **`AddonCookieClient` URL resolution order** (`addon_client.py`): configured `auth_url` → Supervisor API lookup by slug suffix `familysafety-playwright` (HA OS) → `http://localhost:8098` (standalone) → encrypted-file fallback. Don't hardcode the add-on hostname.
- Startup (`rootfs/usr/local/bin/run.sh` for the add-on via s6/bashio, `rootfs-standalone/entrypoint.sh` for plain Docker): Xvfb `:99` → fluxbox → x11vnc (localhost) → noVNC on 6081 → uvicorn on 8098. Chromium runs **non-headless** inside Xvfb so the user can complete the Microsoft login over noVNC.

## Common commands

There is no build/lint/test tooling. Typical operations:

```bash
# Legacy add-on only — the native path needs none of this.
# Run the add-on locally as a plain container (standalone variant)
cd familysafety-playwright
docker compose up -d --build          # builds Dockerfile.standalone, ports 8098 + 6081
docker compose logs -f familysafety-auth

# Add-on HTTP API sanity checks (API key required for cookies/screentime)
curl http://localhost:8098/api/health
curl -H "X-API-Key: $KEY" "http://localhost:8098/api/screentime?childId=<id>"

# HA debug logging (configuration.yaml)
#   logger:
#     logs:
#       custom_components.microsoft_family_safety: debug
#       pyfamilysafety: debug
```

Add-on Python deps: `familysafety-playwright/requirements.txt` (FastAPI/uvicorn/cryptography/pydantic) + `playwright` (installed in the Dockerfile, not in requirements). Integration deps: `manifest.json` `requirements` (`pyfamilysafety==1.1.2`, `cryptography>=3.4.8`).

## pyfamilysafety is pinned and patched

`pyfamilysafety==1.1.2` is the newest published release and it has bugs. `_pyfamilysafety_compat.py` (all patches applied by `apply_patches(hass)` from `coordinator._async_setup_api`, before any auth call) monkey-patches:

- `Authenticator._request_handler` — reuse HA's shared aiohttp session, fixing `'ClientSession' object is not callable` on Python 3.14 (issue #22), and decode JSON defensively against Microsoft HTML error pages (issue #23).
- `Authenticator.perform_refresh` — the upstream version silently returns on a failed token endpoint. The replacement validates status/payload/`expires_in` and raises `HttpException`, and serializes concurrent refreshes on `_login_lock`.
- `FamilySafetyAPI.send_request` — one retry after `perform_refresh()` on `Unauthorized`, dropping the stale `Authorization` header first.
- `FamilySafetyWebAPI._build_headers` — normalize a raw token into `MSAuth1.0 usertoken="…", type="MSACT"`.
- `get_screentime_policy` / `get_web_browsing_settings` / `async_check_web_session` / `connection_state` — see the transport-patch note above; these change the *effective* behaviour away from what the docstrings say.

Separately, `async_approve_request` pre-multiplies `extension_time * 10` to compensate for the library's wrong `* 100` "seconds→ms" conversion (issue #20). Access tokens are refreshed proactively with a 60 s safety margin (`authenticator_access_token_expired`). When touching auth or request handling, keep all of this in mind.

## Release / versioning

Releases are tag-driven (`vX.Y.Z`). On a published GitHub release, `.github/workflows/update-version.yml` rewrites `manifest.json` `version` from the tag, zips the integration, and attaches it; `build-standalone-image.yml` builds and pushes the multi-arch standalone image to `ghcr.io/<owner>/hafamilysafety-auth`. Keep `manifest.json` and add-on `config.json` versions in sync when cutting a release. `.gitattributes` forces LF line endings — relevant because the add-on shell scripts must stay LF (CRLF breaks busybox/s6 in the container).

## Gotchas

- **Day indexing is Microsoft's, not Python's:** `DAY_KEYS` index 0 = **Sunday** … 6 = Saturday (`const.py`). Number/time entities and `set_screentime_*` services use this order.
- **Intervals are 48 half-hour slot booleans** (index 0 = 00:00, 14 = 07:00). `coordinator._range_to_slots` floors the start and ceils the end so the requested window is fully covered.
- **The screen-time write URLs contain a literal double slash** — `/family/api//st/day-allow` and `/family/api//st/day-allow-int`. That is not a typo; do not "fix" it. The read path uses a single slash (`/family/api/st`).
- Cookies/keys live under `/share/familysafety` — a path that only exists on the HA host or in the container. Don't assume it on a dev machine. **This applies to the legacy add-on only**; native auth stores nothing there.
- The integration stays "degraded" (mobile API works, web session missing) rather than failing when web credentials are unavailable; `connection_state()` reports this and a persistent notification (`familysafety_auth_expired`) prompts re-auth. Only a real `LOGIN_REDIRECT`, an independently expired `/account` session, or a Family bootstrap that keeps ending on a login page for two consecutive polls (`family_context_auth_required`) triggers reauth — a bare 401 from the private Family API (`AUTH_ERROR`) while `/account` is still authenticated deliberately does **not** on its own, to avoid a reauth loop caused by a merely stale antiforgery token. `connection_state()` reports `degraded` (not `connected`) whenever `family_context_state` is neither `ready` nor `unknown`, even with `/account` authenticated.
- Backoffs mask failures for a while: 30 min on `/st` timeout/network error (`_family_web_backoff_until`), 30 min on a failed `/account` probe (`_web_probe_backoff_until`). During probe backoff `web_session_state` is `"error"` — deliberately not in `("missing", "expired")`, so it does not trigger reauth. If a change seems to have "no effect", check whether you are inside a backoff window.
- `entity_id`s are legacy-named: no entity sets `_attr_has_entity_name` or `_attr_translation_key`, and every `_attr_name` already embeds the child's name, so HA prefixes the slugified device name and the name appears twice (`number.firstname_lastname_family_safety_firstname_sunday_limit`). Also note the device name differs by platform: `sensor.py`/`number.py`/`time.py` use `"{first} {surname} (Family Safety)"` while `switch.py`/`button.py` use `"{first} (Family Safety)"`. Changing any of this renames entities for every user — treat it as a breaking change.
