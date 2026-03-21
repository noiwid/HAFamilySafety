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

- **Home Assistant** 2024.1.0 or newer
- **HACS** installed ([install guide](https://hacs.xyz/docs/setup/download)) -- recommended
- **Microsoft account** with parent/organizer role in a Family Safety group, with at least one child account and monitored device
- **Python dependency:** `pyfamilysafety==1.1.2` (installed automatically)

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
           ├── api_client.py
           ├── button.py
           ├── config_flow.py
           ├── const.py
           ├── coordinator.py
           ├── manifest.json
           ├── number.py
           ├── sensor.py
           ├── strings.json
           ├── switch.py
           └── translations/
               ├── en.json
               └── fr.json
   ```

3. Restart Home Assistant: **Settings > System > Restart**.

---

## Configuration

### Step 1 -- Add the Integration

1. Go to **Settings > Devices & Services**.
2. Click **+ Add Integration** and search for **Microsoft Family Safety**.

### Step 2 -- Get the Auth URL

The first dialog displays a Microsoft OAuth URL (pointing to `login.live.com` with Family Safety scope).

1. Copy the URL shown in the dialog.
2. Click **Next**.

### Step 3 -- Sign In with Microsoft

1. Open the copied URL in a new browser tab.
2. Sign in with your **parent/organizer** Microsoft account.
3. Accept the permissions prompt if shown.

### Step 4 -- Copy the Redirect URL

After sign-in, your browser will redirect to a **blank page** (or a "can't reach this page" message). This is expected.

1. Look at the browser address bar. The URL will look like:
   ```
   https://login.live.com/oauth20_desktop.srf?code=M.C123_BAY.0.U.-AbCd...&lc=1033
   ```
2. Copy the **entire URL** (including `https://`).

### Step 5 -- Complete Setup

1. Return to the Home Assistant configuration dialog.
2. Paste the full redirect URL into the **Redirect URL** field.
3. Click **Submit**.

The integration will exchange the code for tokens and begin discovering family members and devices.

---

## Options

The integration supports a configurable **update interval** through the options flow.

1. Go to **Settings > Integrations**.
2. Find **Microsoft Family Safety** and click the gear icon (**Configure**).
3. Set the **Update interval** (in seconds):
   - Minimum: **30** seconds
   - Maximum: **3600** seconds (1 hour)
   - Default: **300** seconds (5 minutes)
4. Click **Submit**. The change takes effect immediately.

---

## Verification

1. Go to **Settings > Devices & Services** and click on **Microsoft Family Safety**.
2. Confirm devices and entities have been created.
3. Go to **Settings > Devices & Services > Entities** and search for your child's name. You should see sensors (screen time, account info, applications, per-device screen time), switches (device block/unblock), buttons, and number entities.
4. If entities show "unavailable," wait up to one update interval (default 5 minutes) for the first data pull.

---

## Troubleshooting

### Authentication Failed

- Ensure you copied the **complete** redirect URL, including the `code=` parameter.
- Authorization codes expire within minutes. If you waited too long, restart the flow.
- Use the **parent/organizer** account, not a child account.
- Try an incognito/private browser window.

### No Entities Appear

- Verify your Family Safety setup at [account.microsoft.com/family](https://account.microsoft.com/family) -- at least one child and one monitored device must exist.
- Wait at least one full update interval after setup.
- Check **Settings > System > Logs** for entries containing `microsoft_family_safety`.
- Reload the integration: three-dot menu > **Reload**.

### Token Expired / Reauth Required

The integration includes a reauth flow for expired tokens. When authentication fails, Home Assistant will prompt you to reauthenticate:

1. Go to **Settings > Devices & Services**.
2. Find **Microsoft Family Safety** -- it will show a **Reauthenticate** button.
3. Follow the same auth steps (Steps 2-5 above) to obtain a new token.

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

- **OAuth 2.0:** Authentication uses Microsoft's standard OAuth flow. No passwords are stored.
- **Token storage:** Refresh tokens are stored in Home Assistant's internal configuration store.
- **Scope:** The integration only accesses Microsoft Family Safety data -- no access to email, files, or other services.
- **Unofficial API:** This integration uses an unofficial Microsoft Family Safety API via `pyfamilysafety`. Use at your own risk.
- **Local processing:** All data processing happens within your Home Assistant instance. Data is fetched from Microsoft servers; only tokens are persisted locally.
