# Microsoft Family Safety for Home Assistant

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg?style=for-the-badge)](https://github.com/hacs/integration)
[![GitHub Release](https://img.shields.io/github/release/noiwid/HAFamilySafety.svg?style=for-the-badge)](https://github.com/noiwid/HAFamilySafety/releases)
[![License](https://img.shields.io/github/license/noiwid/HAFamilySafety.svg?style=for-the-badge)](LICENSE)
[![HA Minimum Version](https://img.shields.io/badge/HA-%3E%3D%202023.1-41BDF5?style=for-the-badge)](https://www.home-assistant.io/)

A full-featured Home Assistant custom integration for **Microsoft Family Safety**. Monitor screen time, manage app restrictions, control web filtering, and adjust daily limits — all from your Home Assistant dashboard.

> **Domain:** `microsoft_family_safety` | **IoT Class:** Cloud Polling | **Languages:** English, French

---

## What's New in v1.0

- **15 services** covering app management, platform locking, screen time configuration, web filtering, and more
- **Web API client** for capabilities beyond the pyfamilysafety library (daily limits, time windows, web filters, age ratings, purchase controls)
- **Number entities** for adjusting daily screen time limits per day of the week directly from the UI
- **Switch entities** for blocking apps and locking platforms (Windows, Xbox, Mobile)
- **Button entities** for approving or denying pending screen time extension requests
- **New sensors**: Web Filter status, Screen Time Policy, Pending Requests
- **Per-device entities**: dedicated screen time and info sensors for each physical device
- **Configurable polling interval** (30s to 3600s) via the options flow
- **Full French translations** for config flow, options, and all services

---

## Features

- **Screen time monitoring** — track daily usage per child and per device
- **App management** — block/unblock apps, set per-app time limits and windows
- **Platform control** — lock Windows, Xbox, or Mobile for a specified duration
- **Web filtering** — block/unblock domains, toggle content filtering, set PEGI age ratings
- **Screen time policies** — adjust daily allowances and allowed time intervals
- **Purchase controls** — enable/disable ask-to-buy via service call
- **Request handling** — approve or deny pending screen time requests from HA
- **Hub architecture** — creates HA devices for each child account and each physical device

---

## Installation

### Via HACS (Recommended)

1. Open **HACS** in Home Assistant
2. Go to **Integrations** and click the three-dot menu in the top right
3. Select **Custom repositories**
4. Add `https://github.com/noiwid/HAFamilySafety` with category **Integration**
5. Search for **Microsoft Family Safety** in HACS and click **Download**
6. Restart Home Assistant

### Manual Installation

1. Download the latest release from [GitHub Releases](https://github.com/noiwid/HAFamilySafety/releases)
2. Copy the `custom_components/microsoft_family_safety` folder into your `config/custom_components/` directory
3. Restart Home Assistant

---

## Configuration

### Initial Setup

1. Go to **Settings > Devices & Services > Add Integration**
2. Search for **Microsoft Family Safety**
3. An authentication URL is displayed — copy it and open it in your browser (incognito recommended)
4. Sign in with your **Microsoft parent account**
5. You will be redirected to a blank page. Copy the **entire URL** from the address bar:
   ```
   https://login.live.com/oauth20_desktop.srf?code=M.C123_ABC...&lc=1033
   ```
6. Paste the redirect URL back into the Home Assistant form and submit

The integration will discover all child accounts and their associated devices automatically.

### Options

After setup, go to **Settings > Devices & Services > Microsoft Family Safety > Configure** to adjust:

| Option | Range | Default |
|--------|-------|---------|
| Update interval | 30 – 3600 seconds | 300 seconds (5 min) |

---

## Devices & Entities

The integration creates two types of HA devices:

| Device Type | Name Example | Manufacturer | Model |
|-------------|-------------|--------------|-------|
| Child account | Maceo Collin (Family Safety) | Microsoft | Family Safety Account |
| Physical device | DESKTOP-9N6PNLL | From API | From API |

Physical devices are linked to their parent child account via `via_device`.

### Sensors — Per Child Account

| Entity | Entity ID | State | Key Attributes |
|--------|-----------|-------|----------------|
| Screen Time | `sensor.{name}_screen_time` | Minutes used today | `formatted_time`, `hours`, `minutes`, `seconds`, `average_screentime`, `date` |
| Account Info | `sensor.{name}_account_info` | Full name | `user_id`, `first_name`, `surname`, `profile_picture`, `device_count`, `application_count` |
| Applications | `sensor.{name}_applications` | App count | `blocked_count`, `applications` |
| Balance | `sensor.{name}_balance` | Account balance | *(monetary sensor, only if available)* |
| Pending Requests | `sensor.{name}_pending_requests` | Request count | `requests` |
| Web Filter | `sensor.{name}_web_filter` | enabled / disabled / unknown | `blockedSites`, `allowedSites`, `contentRatingAge` |
| Screen Time Policy | `sensor.{name}_screen_time_policy` | enabled / disabled / unknown | `monday_allowance` ... `sunday_allowance` |

### Sensors — Per Physical Device

| Entity | Entity ID | State | Key Attributes |
|--------|-----------|-------|----------------|
| Device Screen Time | `sensor.{device}_screen_time` | Minutes used today | — |
| Device Info | `sensor.{device}_info` | Device name | `model`, `OS`, `last_seen`, `manufacturer`, `device_class` |

### Switches — Per Child Account

| Entity | Entity ID | Behavior |
|--------|-----------|----------|
| App Block | `switch.{name}_app_{appname}` | ON = app blocked. One switch per application. |
| Platform Lock | `switch.{name}_{platform}_lock` | ON = platform locked for 24h. Platforms: Windows, Xbox, Mobile. |

### Buttons — Per Child Account

| Entity | Entity ID | Action |
|--------|-----------|--------|
| Approve Request | `button.{name}_approve_request` | Approves the oldest pending screen time request (+1 hour) |
| Deny Request | `button.{name}_deny_request` | Denies the oldest pending request |

### Number Entities — Per Child Account

| Entity | Entity ID | Range | Step |
|--------|-----------|-------|------|
| Daily Limit (x7) | `number.{name}_{day}_limit` | 0 – 1440 minutes | 15 min |

One entity per day of the week (Sunday through Saturday). Adjustable directly from the UI.

---

## Services

The integration exposes **15 services**, split between the pyfamilysafety library and the web API.

### App Management

```yaml
# Block an application
service: microsoft_family_safety.block_app
data:
  account_id: "child-account-uuid"
  app_id: "app-uuid"
```

```yaml
# Unblock an application
service: microsoft_family_safety.unblock_app
data:
  account_id: "child-account-uuid"
  app_id: "app-uuid"
```

```yaml
# Set a per-app daily time limit with allowed window
service: microsoft_family_safety.set_app_time_limit
data:
  account_id: "child-account-uuid"
  app_id: "app-uuid"
  app_name: "Minecraft"
  platform: "Windows"
  hours: 1
  minutes: 30
  start_time: "08:00"
  end_time: "20:00"
```

```yaml
# Remove a per-app time limit
service: microsoft_family_safety.remove_app_time_limit
data:
  account_id: "child-account-uuid"
  app_id: "app-uuid"
  app_name: "Minecraft"
  platform: "Windows"
```

### Platform Control

```yaml
# Lock a platform for N hours
service: microsoft_family_safety.lock_platform
data:
  account_id: "child-account-uuid"
  platform: "Xbox"
  duration_hours: 2
```

```yaml
# Unlock a platform
service: microsoft_family_safety.unlock_platform
data:
  account_id: "child-account-uuid"
  platform: "Xbox"
```

### Screen Time

```yaml
# Set daily screen time allowance
service: microsoft_family_safety.set_screentime_limit
data:
  account_id: "child-account-uuid"
  day_of_week: 1  # 0=Sunday, 6=Saturday
  hours: 2
  minutes: 0
```

```yaml
# Set allowed time window (30-min precision)
service: microsoft_family_safety.set_screentime_intervals
data:
  account_id: "child-account-uuid"
  day_of_week: 1
  start_hour: 8
  start_minute: 0
  end_hour: 20
  end_minute: 30
```

### Request Handling

```yaml
# Approve a pending screen time request (+N minutes)
service: microsoft_family_safety.approve_request
data:
  request_id: "request-uuid"
  extension_minutes: 60
```

```yaml
# Deny a pending request
service: microsoft_family_safety.deny_request
data:
  request_id: "request-uuid"
```

### Web Filtering

```yaml
# Block a website
service: microsoft_family_safety.block_website
data:
  account_id: "child-account-uuid"
  website: "example.com"
```

```yaml
# Remove a blocked website
service: microsoft_family_safety.remove_website
data:
  account_id: "child-account-uuid"
  website: "example.com"
```

```yaml
# Toggle web content filtering
service: microsoft_family_safety.toggle_web_filter
data:
  account_id: "child-account-uuid"
  enabled: true
```

### Content & Purchase Controls

```yaml
# Set age rating (PEGI 3-20, or 21 for unrestricted)
service: microsoft_family_safety.set_age_rating
data:
  account_id: "child-account-uuid"
  age: 12
```

```yaml
# Enable or disable ask-to-buy
service: microsoft_family_safety.set_acquisition_policy
data:
  account_id: "child-account-uuid"
  require_approval: true
```

---

## Automation Examples

### Screen Time Alert

Send a notification when a child exceeds 2 hours of screen time:

```yaml
automation:
  - alias: "Screen time limit alert"
    trigger:
      - platform: numeric_state
        entity_id: sensor.maceo_screen_time
        above: 120
    action:
      - service: notify.mobile_app_your_phone
        data:
          title: "Screen Time Alert"
          message: >
            {{ state_attr('sensor.maceo_screen_time', 'formatted_time') }}
            of screen time used today.
```

### Weekday Screen Time Limit

Automatically set a 1.5-hour limit on school days:

```yaml
automation:
  - alias: "Set weekday screen time limits"
    trigger:
      - platform: time
        at: "00:05:00"
    condition:
      - condition: time
        weekday:
          - mon
          - tue
          - wed
          - thu
          - fri
    action:
      - service: microsoft_family_safety.set_screentime_limit
        data:
          account_id: "child-account-uuid"
          day_of_week: "{{ now().weekday() }}"
          hours: 1
          minutes: 30
```

### Dashboard Card

A simple entities card for daily monitoring:

```yaml
type: entities
title: Family Safety
entities:
  - entity: sensor.maceo_screen_time
    name: Screen Time
  - entity: sensor.maceo_pending_requests
    name: Pending Requests
  - entity: sensor.maceo_web_filter
    name: Web Filter
  - type: divider
  - entity: number.maceo_monday_limit
    name: Monday Limit
  - entity: number.maceo_tuesday_limit
    name: Tuesday Limit
```

---

## Troubleshooting

### Authentication Errors

The OAuth token expires after a few weeks. Home Assistant will prompt you to reauthenticate using the same redirect URL flow described above.

### Web API Services Return 401/403

The web API endpoints reuse the Bearer token from pyfamilysafety. If Microsoft rejects it for certain endpoints, the affected services will fail with a 401 or 403 error. Try reauthenticating.

### Data Not Updating

- The default polling interval is 5 minutes. You can lower it to 30 seconds in the integration options.
- Force refresh: **Settings > Devices & Services > Microsoft Family Safety > Reload**.

### Platform Lock Not Working

Device blocking via `lock_platform` / the platform lock switches may not work reliably. This is a known limitation on Microsoft's side — the official Family Safety app has the same issue.

### Debug Logging

Add the following to `configuration.yaml` and restart:

```yaml
logger:
  default: info
  logs:
    custom_components.microsoft_family_safety: debug
    pyfamilysafety: debug
```

Check logs at **Settings > System > Logs**.

---

## Known Limitations

- **Unofficial API** — Microsoft provides no public API for Family Safety. This integration relies on reverse-engineered endpoints that may change or break at any time.
- **Platform locking** may not take effect on the actual device, even though the API accepts the command.
- **Web API authentication** shares the pyfamilysafety token. Some endpoints may reject it depending on Microsoft's server-side validation.

---

## Contributing

Contributions are welcome! See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/my-feature`)
3. Commit your changes and open a pull request

Areas where help is especially appreciated:
- Microsoft API endpoint documentation and analysis
- Authentication improvements (automatic token refresh)
- Additional language translations
- Testing across different Family Safety account configurations

---

## Acknowledgments

- **[pantherale0](https://github.com/pantherale0)** — original [ha-familysafety](https://github.com/pantherale0/ha-familysafety) integration and [pyfamilysafety](https://github.com/pantherale0/pyfamilysafety) library
- The **Home Assistant** community for feedback and testing

---

## License

This project is licensed under the [MIT License](LICENSE).

## Disclaimer

This integration uses an **unofficial, undocumented API** for Microsoft Family Safety. It is not approved, endorsed, or supported by Microsoft. Microsoft may modify or disable the underlying API at any time. Use at your own risk and in compliance with Microsoft's terms of service.

---

## Support

- [GitHub Issues](https://github.com/noiwid/HAFamilySafety/issues)
- [GitHub Discussions](https://github.com/noiwid/HAFamilySafety/discussions)

When reporting an issue, please include: HA version, integration version, debug logs, and steps to reproduce.
