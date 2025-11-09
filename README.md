# Microsoft Family Safety for Home Assistant 👨‍👩‍👧‍👦

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/custom-components/hacs)
[![GitHub release](https://img.shields.io/github/release/noiwid/HAFamilySafety.svg)](https://github.com/noiwid/HAFamilySafety/releases)
[![License](https://img.shields.io/github/license/noiwid/HAFamilySafety.svg)](LICENSE)
[![GitHub stars](https://img.shields.io/github/stars/noiwid/HAFamilySafety.svg)](https://github.com/noiwid/HAFamilySafety/stargazers)

> **Custom integration for Home Assistant to monitor and control Microsoft Family Safety accounts and devices directly from your home automation setup.**

This integration allows you to track your children's screen time, manage device access, and automate parental controls from Home Assistant.

---

## 🌟 Features

### 📊 Account Monitoring
- **Daily screen time tracking** - Precise monitoring of daily usage
- **Average screen time statistics** - Analysis over multiple days
- **Account balance** - Monitor account balance (if enabled in Family Safety)
- **Profile information** - First name, surname, and profile picture

### 📱 Device Management
- **Screen time per device** - Detailed tracking for each device
- **Detailed information** - Model, operating system, last seen
- **Remote lock/unlock** - Complete control over device access
- **Real-time status** - Block status and availability

### 📲 Application Tracking
- **Application counter** - Total number of applications per account
- **Blocked apps list** - Visibility on restrictions
- **Usage monitoring** - Data on application usage

### ⚙️ Available Services
- `block_device` - Block a device immediately or for a defined duration
- `unblock_device` - Unblock a previously blocked device
- `approve_request` - Approve a screen time extension request
- `deny_request` - Deny a screen time extension request

---

## 📸 Screenshots

> 🚧 Section to be completed with screenshots from your installation

---

## 📦 Installation

For detailed installation instructions, see **[INSTALL.md](INSTALL.md)**.

### Quick Start via HACS (Recommended) 🔥

1. Open **HACS** in Home Assistant
2. Click on **"Integrations"**
3. Click the **three dots** in the top right corner
4. Select **"Custom repositories"**
5. Add the repository URL: `https://github.com/noiwid/HAFamilySafety`
6. Category: **"Integration"**
7. Click **"Add"**
8. Search for **"Microsoft Family Safety"** in the list
9. Click **"Download"**
10. **Restart Home Assistant**

### Manual Installation 🛠️

1. Download the latest version from [Releases](https://github.com/noiwid/HAFamilySafety/releases)
2. Copy the `custom_components/microsoft_family_safety` folder to your Home Assistant `custom_components` directory
3. Restart Home Assistant

---

## ⚙️ Configuration

### Initial Configuration

1. Go to **Settings** → **Devices & Services**
2. Click **"+ Add Integration"**
3. Search for **"Microsoft Family Safety"**
4. Follow the authentication steps:
   - Copy the provided Microsoft login URL
   - Open it in your browser
   - Sign in with your Microsoft account (the one managing Family Safety)
   - You'll be redirected to a blank page with a Microsoft message
   - Copy **the complete URL** from your browser's address bar
   - Paste this URL into Home Assistant

### Authentication Process 🔐

The integration uses the Microsoft Family Safety API which requires manual authentication:

1. **Special login URL** - A unique authorization link is generated
2. **Microsoft login** - Use your primary Microsoft account (parent/organizer)
3. **Redirect page** - After login, you'll see a blank page (this is normal!)
4. **Authorization code** - The URL contains a required `code` parameter
5. **Automatic extraction** - The integration extracts the code automatically
6. **Secure storage** - A refresh token is saved securely

**Example redirect URL:**
```
https://login.live.com/oauth20_desktop.srf?code=M.C123_BAY.0.U.-xxxxxxxxxxxxxxxx&lc=1036
```

**Security note** 🔒 : The authorization only grants access to Family Safety data, **NOT** to other Microsoft services like OneDrive, Outlook, etc.

### Configuration Options

- **Update interval** - Default: 5 minutes (300 seconds)
- **Timeout** - API request timeout: 30 seconds

---

## 🎯 Created Entities

### Sensors 📊

#### Per Child Account:

| Entity | Description | Attributes |
|--------|-------------|-----------|
| `sensor.<name>_screen_time` | Daily screen time in minutes | `average_screentime`, `user_id` |
| `sensor.<name>_account_info` | Account information with profile picture | `user_id`, `first_name`, `surname`, `profile_picture`, `device_count`, `application_count` |
| `sensor.<name>_applications` | Number of installed applications | `blocked_count`, `applications` (complete list) |
| `sensor.<name>_balance` | Account balance (if applicable) | Amount in local currency |

#### Per Device:

| Entity | Description | Attributes |
|--------|-------------|-----------|
| `sensor.<device>_screen_time` | Device screen time | `device_id`, `device_name` |
| `sensor.<device>_info` | Device information | `device_id`, `device_name`, `device_model`, `os_name`, `last_seen`, `device_make`, `device_class` |

### Switches 🔄

| Entity | Description | Behavior |
|--------|-------------|----------|
| `switch.<device>` | Device block control | **ON** = Device unblocked (active) ✅<br>**OFF** = Device blocked ❌ |

**Available attributes:**
- `device_id` - Unique device identifier
- `device_name` - Device name
- `device_model` - Model (e.g., iPhone 13)
- `os_name` - Operating system (iOS, Android, Windows, etc.)
- `last_seen` - Last connection
- `blocked` - Block status (true/false)
- `device_make` - Manufacturer
- `device_class` - Device type

---

## 🔧 Services

### `microsoft_family_safety.block_device`

Blocks a device to prevent its use.

**Parameters:**
- `device_id` (required) - Identifier of the device to block
- `duration` (optional) - Block duration in minutes (omit for indefinite block)

**YAML Example:**
```yaml
service: microsoft_family_safety.block_device
data:
  device_id: "abc123def456"
  duration: 60  # Block for 1 hour
```

**Example without duration (permanent block):**
```yaml
service: microsoft_family_safety.block_device
data:
  device_id: "abc123def456"
```

---

### `microsoft_family_safety.unblock_device`

Unblocks a previously blocked device.

**Parameters:**
- `device_id` (required) - Identifier of the device to unblock

**YAML Example:**
```yaml
service: microsoft_family_safety.unblock_device
data:
  device_id: "abc123def456"
```

---

### `microsoft_family_safety.approve_request`

Approves a screen time extension request sent by a child.

**Parameters:**
- `request_id` (required) - Request identifier
- `extension_time` (required) - Additional time granted in minutes

**YAML Example:**
```yaml
service: microsoft_family_safety.approve_request
data:
  request_id: "req_xyz789"
  extension_time: 30  # Grant 30 additional minutes
```

---

### `microsoft_family_safety.deny_request`

Denies a screen time extension request.

**Parameters:**
- `request_id` (required) - Identifier of the request to deny

**YAML Example:**
```yaml
service: microsoft_family_safety.deny_request
data:
  request_id: "req_xyz789"
```

---

## 🤖 Automation Examples

### 🌙 Automatic bedtime lock

Automatically block children's devices at 9:00 PM:

```yaml
automation:
  - alias: "Lock children devices - Bedtime"
    description: "Block all devices at 9:00 PM"
    trigger:
      - platform: time
        at: "21:00:00"
    action:
      - service: switch.turn_off
        target:
          entity_id:
            - switch.lucas_iphone
            - switch.lea_tablet
```

### ☀️ Automatic morning unlock

Unlock devices at 7:00 AM on school days:

```yaml
automation:
  - alias: "Unlock devices - School morning"
    description: "Unlock devices at 7:00 AM on weekdays"
    trigger:
      - platform: time
        at: "07:00:00"
    condition:
      - condition: time
        weekday:
          - mon
          - tue
          - wed
          - thu
          - fri
    action:
      - service: switch.turn_on
        target:
          entity_id:
            - switch.lucas_iphone
            - switch.lea_tablet
```

### ⏰ Excessive screen time alert

Send a notification when screen time exceeds 2 hours:

```yaml
automation:
  - alias: "Excessive screen time alert"
    description: "Notification if more than 2h of screen time"
    trigger:
      - platform: numeric_state
        entity_id: sensor.lucas_screen_time
        above: 120  # 2 hours = 120 minutes
    action:
      - service: notify.mobile_app_parent
        data:
          title: "⚠️ High screen time"
          message: "Lucas exceeded 2 hours of screen time today ({{ states('sensor.lucas_screen_time') }} min)"
```

### 📊 Daily screen time report

Receive a daily report at 8:00 PM:

```yaml
automation:
  - alias: "Daily screen time report"
    description: "Send a screen time summary at 8:00 PM"
    trigger:
      - platform: time
        at: "20:00:00"
    action:
      - service: notify.mobile_app_parent
        data:
          title: "📊 Screen time report"
          message: |
            Screen time today:
            - Lucas: {{ states('sensor.lucas_screen_time') }} min
            - Lea: {{ states('sensor.lea_screen_time') }} min
```

### 🎮 Conditional block based on homework

Block devices if a "homework completed" sensor is OFF:

```yaml
automation:
  - alias: "Block if homework not done"
    description: "Block devices if homework not completed at 6 PM"
    trigger:
      - platform: time
        at: "18:00:00"
    condition:
      - condition: state
        entity_id: input_boolean.lucas_homework_completed
        state: "off"
    action:
      - service: switch.turn_off
        target:
          entity_id: switch.lucas_iphone
      - service: notify.mobile_app_lucas
        data:
          title: "📚 Homework first!"
          message: "Your device is blocked until your homework is completed."
```

### 🔔 Extension request notification

Use a webhook to be notified of extension requests (requires additional configuration):

```yaml
automation:
  - alias: "Extension request notification"
    description: "Alert when a child requests more time"
    trigger:
      - platform: event
        event_type: microsoft_family_safety_request
    action:
      - service: notify.mobile_app_parent
        data:
          title: "⏱️ Extension request"
          message: "{{ trigger.event.data.child_name }} requests {{ trigger.event.data.extension_minutes }} additional minutes"
          data:
            actions:
              - action: "APPROVE_REQUEST"
                title: "✅ Approve"
              - action: "DENY_REQUEST"
                title: "❌ Deny"
```

---

## 🔍 Troubleshooting

### ❌ Authentication error / Token expired

**Symptom:** Authentication error messages in logs, data not updating

**Solution:**
1. Go to **Settings** → **Devices & Services**
2. Find the **Microsoft Family Safety** integration
3. Click **"Configure"**
4. Follow the authentication process again with a new code

### 📭 No data displayed

**Symptoms:** Integration installed but no entities created or zero data

**Solutions:**
- ✅ Verify that you have configured Family Safety in your Microsoft account
- ✅ Ensure that devices are registered in Family Safety
- ✅ Wait a few minutes for the first sync (up to 5 minutes)
- ✅ Check Home Assistant logs for error messages

**Verification:**
```bash
# In Home Assistant logs, search for:
[custom_components.microsoft_family_safety]
```

### 🔌 Devices not responding to commands

**Symptoms:** The switch doesn't block/unblock the device

**Solutions:**
- ✅ Verify that the device is online and connected to the Internet
- ✅ Ensure that Family Safety is enabled on the device
- ✅ Verify that the device is not in "Local management only" mode
- ✅ Try refreshing the integration (restart or reload)
- ✅ Test block/unblock from the Microsoft Family Safety app to confirm the API is working

### ⚠️ Error "pyfamilysafety not found"

**Symptom:** Error when loading the integration

**Solution:**
1. Verify that the integration version is compatible with your Home Assistant version
2. Try completely restarting Home Assistant
3. If the problem persists, reinstall the integration via HACS

### 🐛 Debug logs

To enable detailed logs, add to `configuration.yaml`:

```yaml
logger:
  default: info
  logs:
    custom_components.microsoft_family_safety: debug
    pyfamilysafety: debug
```

Then restart Home Assistant and check the logs in **Settings** → **System** → **Logs**.

---

## 🏗️ Technical Architecture

### Components

- **`__init__.py`** - Integration initialization, service registration
- **`config_flow.py`** - Configuration flow and authentication
- **`coordinator.py`** - Data update coordination via API
- **`sensor.py`** - Sensor entities (screen time, account info, devices)
- **`switch.py`** - Switch entities (device block/unblock)
- **`const.py`** - Constants and configuration
- **`manifest.json`** - Integration metadata

### Dependencies

- **pyfamilysafety 1.1.2** - Python library for Microsoft Family Safety API
- Home Assistant Core >= 2024.1.0 (recommended)

### Data updates

- **Default interval:** 5 minutes (300 seconds)
- **Method:** Cloud polling (iot_class: cloud_polling)
- **Type:** Hub integration (manages multiple devices/accounts)

---

## ❓ Frequently Asked Questions (FAQ)

### Can I manage multiple families?

No, currently the integration only supports one Microsoft Family Safety account at a time. However, you can configure multiple instances if you have multiple accounts.

### Is data stored locally?

Only the refresh token is stored locally in a secure manner. All data is retrieved in real-time from Microsoft servers.

### What is the update frequency?

By default, data is updated every 5 minutes. This value can be adjusted in the integration options.

### Is it compatible with all Family Safety devices?

Yes, as long as the device is visible in the Microsoft Family Safety app, it will appear in Home Assistant.

### Does the integration work offline?

No, an Internet connection is required as data comes from Microsoft servers.

---

## 🤝 Contributions

Contributions are welcome! Feel free to:

- 🐛 Report bugs via [Issues](https://github.com/noiwid/HAFamilySafety/issues)
- 💡 Propose new features
- 🔧 Submit Pull Requests
- 📖 Improve documentation
- ⭐ Star the project if you like it!

---

## 💎 Credits

### Developers

- **[@noiwid](https://github.com/noiwid)** - Main developer

### Libraries used

- **[pyfamilysafety](https://github.com/pantherale0/pyfamilysafety)** by [@pantherale0](https://github.com/pantherale0) - Python wrapper for Microsoft Family Safety API

### Inspirations

- **[HAFamilyLink](https://github.com/noiwid/HAFamilyLink)** - Similar integration for Google Family Link

---

## 📄 License

This project is licensed under the **MIT License** - see the [LICENSE](LICENSE) file for details.

```
MIT License - Copyright (c) 2025 noiwid

Permission is hereby granted to use, copy, modify, and distribute
subject to inclusion of the copyright notice and this permission notice.
```

---

## ⚖️ Legal Disclaimer

This integration uses **unofficial APIs** from Microsoft Family Safety. It is **not affiliated with, endorsed, or sponsored by Microsoft Corporation**.

**Use at your own risk:**
- ⚠️ Microsoft may modify or restrict API access at any time
- ⚠️ No guarantee of availability or stability is provided
- ⚠️ Use this integration responsibly and in compliance with Microsoft's terms of service

---

## 📞 Support

### Need help?

- 📖 **Documentation** : Read this README in full
- 🐛 **Bugs** : [Create an issue](https://github.com/noiwid/HAFamilySafety/issues)
- 💬 **Questions** : [GitHub Discussions](https://github.com/noiwid/HAFamilySafety/discussions)
- 🏠 **Home Assistant Forum** : [Community Forum](https://community.home-assistant.io/)

### Useful information for support

When reporting an issue, please provide:
- Home Assistant version
- Microsoft Family Safety integration version
- Relevant logs (with debug level enabled)
- Detailed problem description
- Steps to reproduce

---

## 🌍 Languages

- 🇬🇧 English (this document)
- 🇫🇷 Français (version française disponible)

---

## 📊 Project Statistics

![GitHub stars](https://img.shields.io/github/stars/noiwid/HAFamilySafety?style=social)
![GitHub forks](https://img.shields.io/github/forks/noiwid/HAFamilySafety?style=social)
![GitHub watchers](https://img.shields.io/github/watchers/noiwid/HAFamilySafety?style=social)

---

**Made with ❤️ for the Home Assistant community**

> If this integration is useful to you, consider giving it a ⭐ on GitHub!
