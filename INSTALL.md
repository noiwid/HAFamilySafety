# Microsoft Family Safety - Installation Guide

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=noiwid&repository=HAFamilySafety&category=Integration)

## Table of Contents

- [Prerequisites](#prerequisites)
- [Installation](#installation)
  - [HACS (Recommended)](#hacs-recommended)
  - [Manual Installation](#manual-installation)
- [Configuration](#configuration)
- [Options](#options)
- [Verification](#verification)
- [Troubleshooting](#troubleshooting)
- [Security & Privacy](#security--privacy)

---

## Prerequisites

- **Home Assistant** 2024.1.0 or newer (any installation type: OS, Supervised, Container, Core)
- A Home Assistant URL reachable from your browser over **HTTPS**. If your instance is HTTP-only on the local network, see [Insecure local HTTP](#insecure-local-http)
- **HACS** installed ([install guide](https://hacs.xyz/docs/setup/download)) -- recommended
- **Microsoft account** with parent/organizer role in a Family Safety group, with at least one child account and monitored device
- **Python dependencies:** `pyfamilysafety==1.1.2`, `cryptography>=3.4.8` (installed automatically)

> **The Playwright auth add-on is no longer required.** Authentication happens natively inside Home Assistant. The add-on remains supported as a legacy fallback -- see [Legacy add-on mode](#legacy-add-on-mode).

> **Upgrading from an add-on based setup?** Re-adding the integration creates new entity IDs. Read the *Breaking changes* section of the [README](README.md#breaking-changes) first.

---

## Installation

### HACS (Recommended)

1. Open **HACS** > **Integrations**.
2. Click the three-dot menu in the top-right corner and select **Custom repositories**.
3. Add the repository:
   - **URL:** `https://github.com/noiwid/HAFamilySafety`
   - **Category:** Integration
4. Click **Add**, then close the dialog.
5. Click **+ Explore & Download Repositories**, search for **Microsoft Family Safety**, and download it.
6. Restart Home Assistant: **Settings > System > Restart**.

### Manual Installation

1. Download the [latest release](https://github.com/noiwid/HAFamilySafety/releases) and extract it.
2. Copy the `custom_components/microsoft_family_safety/` folder into your Home Assistant `config/custom_components/` directory.

   Expected directory structure:

   ```
   config/
   └── custom_components/
       └── microsoft_family_safety/
           ├── __init__.py
           ├── _httpx_web_adapter.py
           ├── _httpx_web_tuning.py
           ├── _pyfamilysafety_compat.py
           ├── api_client.py
           ├── auth/
           │   ├── addon_client.py
           │   └── native_proxy.py
           ├── button.py
           ├── config_flow.py
           ├── const.py
           ├── coordinator.py
           ├── manifest.json
           ├── number.py
           ├── sensor.py
           ├── services.yaml
           ├── strings.json
           ├── switch.py
           ├── time.py
           └── translations/
               ├── de.json
               ├── en.json
               └── fr.json
   ```

3. Restart Home Assistant: **Settings > System > Restart**.

---

## Configuration

### Step 1 -- Add the Integration

1. Go to **Settings > Devices & Services**.
2. Click **+ Add Integration** and search for **Microsoft Family Safety**.

### Step 2 -- Set the Basic Options

The first dialog asks for:

| Field | Default | Notes |
|-------|---------|-------|
| Update interval (seconds) | 300 | 30 -- 3600 |
| Monitored platforms | Windows | Windows / Xbox / Mobile |
| Allow insecure local HTTP authentication (testing only) | off | See [below](#insecure-local-http) |
| Legacy auth add-on URL (optional) | *(empty)* | Only for [legacy mode](#legacy-add-on-mode) |
| Legacy add-on API key (optional) | *(empty)* | Only for legacy mode |

Leave the two legacy fields empty unless you deliberately want add-on mode. Click **Submit**.

### Step 3 -- Sign In to Microsoft

Home Assistant shows an **Open website** button.

1. Click it. A browser window opens on the Microsoft sign-in page, served through a temporary authentication proxy that Home Assistant mounts inside its own HTTP server.
2. Sign in with your **parent/organizer** Microsoft account (not a child account) and complete MFA if prompted.
3. Accept the permissions prompt if shown.

You do **not** copy an authorization URL and you do **not** paste a redirect URL back. Home Assistant captures the OAuth redirect itself.

### Step 4 -- Let the Family Session Complete

**Keep the browser window open.** After the visible sign-in finishes, Home Assistant establishes the Family web session. Two things can happen:

- In most cases Home Assistant completes it **server-side** and you see nothing further.
- Otherwise, the same tab is redirected again so Microsoft's silent Family SSO can complete. Home Assistant switches to a waiting screen. **This can take up to about 60 seconds.** Do not press *Open website* again while it is in progress.

Both paths are normal. The two-phase design is required because Microsoft gates the Family dashboard behind an interactive OAuth step that cannot be replayed purely server-side from a cold start.

### Step 5 -- Finish

When both phases complete, the browser window closes itself and Home Assistant shows **Microsoft Family Safety sign-in completed**. Click **Continue**.

The integration discovers family members and devices automatically.

---

## Options

**Settings > Devices & Services > Microsoft Family Safety > Configure**:

| Option | Range / Format | Default |
|--------|----------------|---------|
| Update interval (seconds) | 30 -- 3600 | 300 |
| Monitored platforms | Windows / Xbox / Mobile | Windows |
| Allow insecure local HTTP authentication (testing only) | on / off | off |
| Legacy auth add-on URL (optional) | `http://HOST:8098` | *(empty)* |
| Legacy add-on API key (optional) | string | *(empty)* |

Changes take effect immediately.

### Insecure local HTTP

Native authentication proxies your Microsoft credentials through Home Assistant, so it requires an **HTTPS** Home Assistant URL by default. If Home Assistant is only reachable over plain HTTP, you may tick **Allow insecure local HTTP authentication (testing only)**.

It is accepted **only** for `localhost`, `homeassistant`, `*.local`, or a private / loopback / link-local IP address. A public HTTP URL is refused with:

> Insecure HTTP authentication is allowed only through localhost, .local, or a private/local IP address.

While enabled, your Microsoft password and session data travel **unencrypted across your local network**, and a WARNING is logged for every sign-in. Treat it as a testing option; configuring HTTPS is the better answer.

### Legacy add-on mode

If you already run the Playwright auth add-on (or the standalone Docker container) and prefer to keep it, set the **Legacy auth add-on URL** option -- or simply have the add-on running, in which case it is auto-detected during setup. Screen time reads and writes are then routed through the add-on's browser session, exactly as before.

An entry runs in native mode as soon as it has a natively captured web session, and in legacy mode otherwise. The two modes are mutually exclusive per config entry.

---

## Verification

1. Go to **Settings > Devices & Services** and click on **Microsoft Family Safety**.
2. Confirm devices and entities have been created.
3. Check the **Connection** diagnostic sensor. It should read `connected`. `degraded` means the mobile API works but the Family web session does not -- screen time schedules will show as `unknown`.
4. Go to **Settings > Devices & Services > Entities** and search for your child's name. You should see sensors, switches, buttons, 7 number entities (daily limits) and 14 time entities (7 start + 7 end).
5. If entities show "unavailable," wait up to one update interval (default 5 minutes) for the first data pull.

> Entity IDs are prefixed with the device name, e.g. `number.firstname_lastname_family_safety_firstname_sunday_limit`. See the [README](README.md#breaking-changes).

---

## Troubleshooting

### "Automatic browser authentication normally requires HTTPS"

No HTTPS Home Assistant URL is configured. Configure HTTPS, or enable [Insecure local HTTP](#insecure-local-http).

### The sign-in window returns "400 Bad Request"

Home Assistant's `security_filter` middleware rejects query strings matching a file-injection pattern, and some of Microsoft's silent-SSO redirects (`epctrc=/w/...`) match it. The integration ships a workaround scoped to its own proxy routes only, but the problem is **intermittent** -- it depends on the OAuth path Microsoft picks. Retry the flow, and check the logs for `security_filter` entries.

### Sign-in never completes

- Phase B can take up to about **60 seconds**. Keep the window open, and do not click *Open website* again.
- The authentication proxy expires after **10 minutes**. If you took longer, restart the flow.
- *"The browser authentication flow expired. Please start again."* -- restart the flow.

### Wrong account

*"A different Microsoft account was used."* -- reauthentication must use the same Microsoft account the entry was created with.

### No Entities Appear

- Verify your Family Safety setup at [account.microsoft.com/family](https://account.microsoft.com/family) -- at least one child and one monitored device must exist.
- Wait at least one full update interval after setup.
- Check **Settings > System > Logs** for entries containing `microsoft_family_safety`.
- Reload the integration: three-dot menu > **Reload**.

### Session Expired / Reauthentication Required

The integration cannot renew the Family web session on its own. When it expires, Home Assistant raises a reauthentication prompt and a persistent notification.

1. Go to **Settings > Devices & Services**.
2. Find **Microsoft Family Safety** -- it shows a **Reauthenticate** button.
3. Follow the same sign-in steps. Both the mobile refresh token and the Family web session are renewed.

### Debug Logging

Add the following to `configuration.yaml` and restart Home Assistant:

```yaml
logger:
  default: info
  logs:
    custom_components.microsoft_family_safety: debug
    pyfamilysafety: debug
```

Check logs at **Settings > System > Logs**. Include these logs when filing [issues on GitHub](https://github.com/noiwid/HAFamilySafety/issues).

---

## Security & Privacy

- **OAuth 2.0 and browser sign-in:** Authentication uses Microsoft's own sign-in pages. Your password is never stored, never logged, and never persisted by the integration.
- **The sign-in is proxied through Home Assistant.** Microsoft's login page is served from your Home Assistant origin, so the address bar shows your Home Assistant URL rather than `login.live.com` and you cannot check the Microsoft certificate visually. Only ever start the flow from Home Assistant itself.
- **Stored credentials are not encrypted.** Microsoft session cookies, the Family antiforgery token and the OAuth refresh token are written to the config entry and to `.storage/` in **clear text**. The legacy add-on encrypted its cookie file with Fernet; the native path does not. Anyone able to read your configuration directory -- including anyone holding an unencrypted backup -- can extract a usable Microsoft session. Protect and encrypt your backups.
- **The authentication proxy endpoint is unauthenticated.** `/auth/microsoft_family_safety/proxy/{token}` and its callback are registered without Home Assistant authentication; the only guard is a 24-byte random token in the URL. It only forwards to `live.com`, `microsoft.com` and `microsoftonline.com`, it lives at most **10 minutes**, and it is destroyed when the flow ends.
- **Security filter:** the integration installs a middleware that bypasses Home Assistant's `security_filter` query-string check **for its own proxy routes only**. The global filter stays active for every other endpoint.
- **Scope:** The integration only accesses Microsoft Family Safety data -- no access to email, files, or other services.
- **Unofficial API:** This integration uses unofficial Microsoft Family Safety APIs. Use at your own risk.
- **Local processing:** All data processing happens within your Home Assistant instance. Data is fetched from Microsoft servers; only tokens and cookies are persisted locally.
