# Microsoft Family Safety for Home Assistant 👨‍👩‍👧‍👦

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![GitHub Release](https://img.shields.io/github/release/noiwid/HAFamilySafety.svg)](https://github.com/noiwid/HAFamilySafety/releases)
[![License](https://img.shields.io/github/license/noiwid/HAFamilySafety.svg)](LICENSE)

> **Read-only monitoring integration for Home Assistant to track Microsoft Family Safety accounts and device usage.**

This integration allows you to monitor your children's screen time and device usage from Home Assistant. **Note: Remote device control is not functional.**

---

## ⚠️ IMPORTANT LIMITATIONS

### What Works ✅

- **Screen time monitoring** - Today's usage and daily averages
- **Last usage tracking** - Date and time of last device connection
- **Account information** - First name, surname, and profile picture
- **Account balance** - Allowance tracking (if enabled)
- **Device list** - All devices associated with child accounts
- **Application data** - Installed applications and their status

### What Does NOT Work ❌

**Remote device control (blocking/unblocking) does not work.**

- API commands are accepted (HTTP 201 status)
- Status changes in Home Assistant
- **BUT devices don't actually block/unblock**

### Why This Limitation?

This is a **Microsoft Family Safety limitation**, not an integration issue:

1. **No official API** - Microsoft provides no public API for Family Safety
2. **Disabled functionality** - The "Lock device" button in Microsoft's official app no longer works
3. **Original integration archived** - The [ha-familysafety](https://github.com/pantherale0/ha-familysafety) integration by pantherale0 was archived in October 2025, likely for the same reasons

This integration uses an **unofficial, undocumented API** discovered through reverse engineering. Microsoft may modify or disable it at any time.

---

## 📦 Installation

For detailed installation instructions, see **[INSTALL.md](INSTALL.md)**.

### Via HACS (Recommended) 🔥

1. Open **HACS** in Home Assistant
2. Click on **Integrations**
3. Click the **⋮** menu in the top right corner
4. Select **Custom repositories**
5. Add the repository URL: `https://github.com/noiwid/HAFamilySafety`
6. Category: **Integration**
7. Click **Add**
8. Search for **"Microsoft Family Safety"** in HACS
9. Click **Download**
10. **Restart Home Assistant**

### Manual Installation 🛠️

1. Download the latest release from [GitHub Releases](https://github.com/noiwid/HAFamilySafety/releases)
2. Extract the `custom_components/microsoft_family_safety` folder to your `config/custom_components/` directory
3. Restart Home Assistant

---

## ⚙️ Configuration

### 1. Obtain Microsoft Token

1. Go to [https://familysafety.microsoft.com](https://familysafety.microsoft.com)
2. Log in with your Microsoft account (parent account)
3. Open **Developer Tools** in your browser (F12)
4. Go to the **Network** tab
5. Refresh the page
6. Look for a request to `mobileaggregator.family.microsoft.com`
7. Click on it and go to the **Headers** tab
8. Find the **Cookie** header and copy the `wl_at` value

**Example cookie:**
```
wl_at=3.1.0.0.3f20fbf...
```

### 2. Configure the Integration

1. Go to **Settings** → **Devices & Services**
2. Click **+ Add Integration**
3. Search for **"Microsoft Family Safety"**
4. Paste your `wl_at` token
5. Click **Submit**

The integration will automatically discover all child accounts and their devices.

---

## 📊 Available Sensors

### Per Child Account

- **Screen Time** (`sensor.{name}_screen_time`)
  - Today's usage in seconds
  - Attributes: `formatted_time`, `hours`, `minutes`, `seconds`, `average_screentime`

- **Account Info** (`sensor.{name}_account_info`)
  - Full name
  - Attributes: Profile picture, device count, application count

- **Applications** (`sensor.{name}_applications`)
  - Total application count
  - Attributes: Blocked app count, full application list

- **Balance** (`sensor.{name}_balance`) *(if enabled)*
  - Account balance/allowance
  - Currency unit

### Per Device

- **Device Screen Time** (`sensor.{device}_screen_time`)
  - Time used today in seconds
  - Attributes: `formatted_time`, `hours`, `minutes`, `seconds`

- **Device Info** (`sensor.{device}_info`)
  - Device name
  - Attributes: Model, OS, last seen, manufacturer, device class

---

## 💡 Example Automations

### Alert on Excessive Screen Time

```yaml
automation:
  - alias: "Screen Time Alert"
    trigger:
      - platform: numeric_state
        entity_id: sensor.child_screen_time
        above: 7200  # 2 hours
    action:
      - service: notify.mobile_app
        data:
          title: "⚠️ Screen Time Alert"
          message: "Child has exceeded 2 hours of screen time today"
```

### Dashboard Card

```yaml
type: entities
title: Family Screen Time
entities:
  - entity: sensor.child_screen_time
    name: Today
  - entity: sensor.child_screen_time
    type: attribute
    attribute: average_screentime
    name: Daily Average
```

### History Graph

```yaml
type: history-graph
title: Screen Time Evolution
entities:
  - sensor.child_screen_time
hours_to_show: 168  # 7 days
```

---

## 🔧 Troubleshooting

### Token Expires

The `wl_at` token expires after a few weeks/months. If you see authentication errors:

1. Obtain a new token (see Configuration section)
2. Go to **Settings** → **Devices & Services** → **Microsoft Family Safety**
3. Click **Configure** and paste the new token

### Data Not Updating

- The integration polls the API every **5 minutes**
- You can force an update: **Settings** → **Devices & Services** → **Microsoft Family Safety** → **⋮** → **Reload**

### Debug Logs

To enable detailed logging, add to `configuration.yaml`:

```yaml
logger:
  default: info
  logs:
    custom_components.microsoft_family_safety: debug
    pyfamilysafety: debug
```

Then restart Home Assistant and check **Settings** → **System** → **Logs**.

---

## 🤝 Contributing

**This integration needs your help!**

### Known Issues Needing Contributors

#### 1. Device Control 🔐

Remote blocking/unblocking doesn't work.

**What's needed:**
- Network traffic analysis of Microsoft Family Safety mobile app
- Reverse engineering of device control API endpoints
- Testing different request structures
- API documentation

**Required skills:**
- Python
- Network analysis (Wireshark, mitmproxy, Charles Proxy)
- REST API reverse engineering
- Testing and debugging

#### 2. API Documentation 📚

Microsoft Family Safety API is not publicly documented.

**What's needed:**
- Complete endpoint mapping
- Request/response structure documentation
- Rate limits and quota identification
- Error code documentation

**Required skills:**
- Technical writing
- API analysis
- Python (for testing)

#### 3. Authentication Improvements 🔑

Current system requires manual token retrieval.

**What's needed:**
- Automatic token refresh implementation
- Full OAuth2 flow support
- Better authentication error handling
- Authentication process documentation

**Required skills:**
- OAuth2 / JWT
- Python
- Home Assistant config flow
- Security best practices

### How to Contribute

1. **Fork** the project
2. **Create a branch** for your feature (`git checkout -b feature/AmazingFeature`)
3. **Commit** your changes (`git commit -m 'Add some AmazingFeature'`)
4. **Push** to the branch (`git push origin feature/AmazingFeature`)
5. **Open a Pull Request**

For more details, see [CONTRIBUTING.md](CONTRIBUTING.md).

### Useful Resources

- [Home Assistant Developer Documentation](https://developers.home-assistant.io/)
- [pyfamilysafety library](https://github.com/pantherale0/pyfamilysafety)
- [Home Assistant Integration Blueprint](https://github.com/custom-components/blueprint)
- [Burp Suite](https://portswigger.net/burp) / [Charles Proxy](https://www.charlesproxy.com/) for network analysis

---

## 📝 License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for more information.

---

## ⚖️ Disclaimer

This integration uses an **unofficial API** for Microsoft Family Safety. It is neither approved nor supported by Microsoft.

- ⚠️ Microsoft may modify or disable the API at any time
- ⚠️ Use at your own risk
- ⚠️ No guarantee of functionality is provided
- ⚠️ Use this integration responsibly and in compliance with Microsoft's terms of service

---

## 🙏 Acknowledgments

- **[pantherale0](https://github.com/pantherale0)** for the original [ha-familysafety](https://github.com/pantherale0/ha-familysafety) integration and [pyfamilysafety](https://github.com/pantherale0/pyfamilysafety) library
- The **Home Assistant** community for support and feedback

---

## 📞 Support

- **Issues**: [GitHub Issues](https://github.com/noiwid/HAFamilySafety/issues)
- **Discussions**: [GitHub Discussions](https://github.com/noiwid/HAFamilySafety/discussions)
- **Home Assistant Forum**: [Community Forum](https://community.home-assistant.io/)

### When Reporting Issues

Please provide:
- Home Assistant version
- Microsoft Family Safety integration version
- Relevant logs (with debug enabled)
- Detailed problem description
- Steps to reproduce

---

**Made with ❤️ for the Home Assistant community**

> If this integration is useful to you, consider giving it a ⭐ on GitHub!
