## Highlights

### Dual-component architecture

This release introduces a **companion add-on** (`familysafety-playwright`) that runs a headless Chromium browser to handle Microsoft web API authentication. The integration and addon work together — the addon maintains an authenticated browser session, and the integration routes API calls through it.

**Why?** Microsoft Family Safety has two APIs with different auth methods. The mobile API (OAuth token) handles basic reads, but **screen time schedule modifications only work through the web API**, which requires browser cookies and a CSRF token extracted from the page DOM. The addon solves this transparently.

### Account Lock — the headline feature

The original per-platform lock (Windows/Xbox/Mobile) relied on a Microsoft API that **no longer works**. This release introduces a new **Account Lock** that actually works by manipulating screen time quotas:

- `switch.<name>_lock` — a single toggle per child
- **ON** = all 7-day quotas set to 0, all time intervals blocked
- **OFF** = previously saved quotas restored automatically
- **Persists across HA restarts** (saved via HA native `.storage/`)
- **Optimistic updates** — UI reflects the change immediately
- Works with automations, watchdogs, schedules

### Screen time control from the UI

Daily limits and allowed time windows are fully adjustable from the dashboard:

- **Number entities** (`number.<name>_monday_limit` ... `sunday_limit`) — set daily allowance in minutes
- **Time entities** (`time.<name>_monday_start/end` ... `sunday_start/end`) — set allowed screen time window
- **Optimistic updates** — values reflect instantly, revert on failure
- All writes routed through the addon browser session (web API `POST /family/api//st/day-allow`)

### 17 services

Full control over Microsoft Family Safety from HA services:

| Category | Services |
|----------|----------|
| Account Lock | `lock_account`, `unlock_account` |
| App Management | `block_app`, `unblock_app`, `set_app_time_limit`, `remove_app_time_limit` |
| Screen Time | `set_screentime_limit`, `set_screentime_intervals` |
| Web Filtering | `block_website`, `remove_website`, `toggle_web_filter` |
| Content & Purchases | `set_age_rating`, `set_acquisition_policy` |
| Requests | `approve_request`, `deny_request` |
| Platform *(deprecated)* | `lock_platform`, `unlock_platform` |

### Entity coverage

| Type | Entities |
|------|----------|
| Sensors | Screen Time, Account Info, Applications, Balance, Pending Requests, Web Filter, Screen Time Policy + per-device sensors |
| Switches | **Account Lock** (optimistic), App Block (per app), Platform Lock *(deprecated)* |
| Numbers | Daily screen time limit x7 days (optimistic) |
| Time | Interval start/end x7 days (optimistic) |
| Buttons | Approve / Deny pending requests |

## What changed since v0.6.0

### New features
- **Companion add-on** (`familysafety-playwright`) with headless Chromium for web API authentication
- **Account Lock switch** with save/restore of screen time policies
- **Screen time reads** via addon browser session (cookie + CSRF based)
- **Screen time writes** via addon browser POST (`/family/api//st/day-allow`, `/family/api//st/day-allow-int`)
- **Number entities** for daily screen time limits (adjustable from UI)
- **Time entities** for screen time interval windows (start/end per day)
- **Optimistic updates** on lock switch, number entities, and time entities
- **Dynamic addon hostname resolution** via Supervisor API (portable across installations)
- **Hub architecture** with HA devices per child account and per physical device
- **Configurable polling interval** (30s-3600s) via options flow
- **Full French translations** for all config, options, and services

### Reliability improvements
- **Browser session management** — cookies injected before navigation, OAuth silent flow handled automatically
- **CSRF token** extracted from DOM hidden input (not cookie) — matches what Microsoft expects
- **Correct API headers** (`X-AMC-JsonMode`, `X-Requested-With: XMLHttpRequest`)
- Lock/unlock uses **best-effort recovery** per day (no more silent partial failures)
- `BaseException` catch in browser context methods guarantees cleanup on `CancelledError`
- HTTP response bodies drained on error to prevent file descriptor leaks
- `web_api` reset on auth failure for proper reinitialization
- Number/Time entities raise `HomeAssistantError` with clear messages on failure
- Transient API errors during setup now retry instead of blocking the integration

### Deprecations
- `lock_platform` / `unlock_platform` services and per-platform lock switches are **deprecated** (Microsoft broke the underlying API). Use Account Lock instead.

## Automation example

```yaml
# Lock at 21:00 on school nights, unlock at 07:00
automation:
  - alias: "Lock account at night"
    trigger:
      - platform: time
        at: "21:00:00"
    condition:
      - condition: time
        weekday: [sun, mon, tue, wed, thu]
    action:
      - action: switch.turn_on
        target:
          entity_id: switch.maceo_lock

  - alias: "Unlock account in the morning"
    trigger:
      - platform: time
        at: "07:00:00"
    action:
      - action: switch.turn_off
        target:
          entity_id: switch.maceo_lock
```

### Requirements
- Home Assistant 2024.1.0 or newer
- Home Assistant OS or Supervised (for the companion add-on)
- pyfamilysafety==1.1.2

### Known limitations
- **Unofficial API** — Microsoft may change or break endpoints at any time
- **Addon required** — Screen time reads and writes need the companion addon with its browser session
- **Account Lock is account-wide** — locks all platforms simultaneously (no per-platform granularity via web API)
- **Lock speed** — 14 sequential API calls via browser (~10-20 seconds)
- **One request at a time** — the addon uses a single browser instance with a lock; concurrent requests are queued
- **Session maintenance** — Microsoft sessions can expire; occasional re-authentication via noVNC may be needed
- If HA storage is cleared while locked, unlock restores safe defaults (2h/day, 07:00-22:00)
