# 🎉 Microsoft Family Safety v0.5.0 - Initial Release

First public release of the Microsoft Family Safety integration for Home Assistant!

## 🌟 Features

### 📊 Account Monitoring
- **Daily screen time tracking** - Monitor daily usage for each child account
- **Average screen time statistics** - Analyze usage trends over multiple days
- **Account balance** - Track account balance when enabled in Family Safety
- **Profile information** - Display first name, surname, and profile picture
- **Application tracking** - Count and list all installed applications per account

### 📱 Device Management
- **Screen time per device** - Detailed tracking for each registered device
- **Device information** - Model, operating system, last seen timestamp
- **Remote lock/unlock** - Control device access via switches
- **Real-time status** - Monitor block status and device availability
- **Multiple device support** - Manage all devices registered in Family Safety

### 🎯 Entities Created

**Sensors** (per child account):
- `sensor.<name>_screen_time` - Daily screen time in minutes
- `sensor.<name>_account_info` - Account details with profile picture
- `sensor.<name>_applications` - Application count and complete list
- `sensor.<name>_balance` - Account balance (if applicable)

**Sensors** (per device):
- `sensor.<device>_screen_time` - Device-specific screen time
- `sensor.<device>_info` - Detailed device information

**Switches** (per device):
- `switch.<device>` - Device control (ON = unblocked, OFF = blocked)

### ⚙️ Services

- `microsoft_family_safety.block_device` - Block a device (with optional duration)
- `microsoft_family_safety.unblock_device` - Unblock a previously blocked device
- `microsoft_family_safety.approve_request` - Approve screen time extension requests
- `microsoft_family_safety.deny_request` - Deny screen time extension requests

## 🔐 Authentication

- Secure **OAuth 2.0** authentication flow with Microsoft
- **Refresh token** storage for automatic re-authentication
- Step-by-step authentication guide in documentation
- Support for re-authentication when tokens expire

## 📦 Installation

### HACS (Recommended)
1. Add custom repository: `https://github.com/noiwid/HAFamilySafety`
2. Search for "Microsoft Family Safety"
3. Download and install
4. Restart Home Assistant

### Manual Installation
1. Download and extract the latest release
2. Copy `custom_components/microsoft_family_safety` to your Home Assistant config
3. Restart Home Assistant

See [INSTALL.md](https://github.com/noiwid/HAFamilySafety/blob/main/INSTALL.md) for detailed instructions.

## 📖 Documentation

- **README.md** - Complete integration documentation with automation examples
- **INSTALL.md** - Step-by-step installation and configuration guide
- **French and English translations** - Full UI support for both languages

## 🔧 Technical Details

- **Home Assistant Core** >= 2024.1.0 (recommended)
- **pyfamilysafety** 1.1.2 (pinned for stability)
- **Cloud polling** - 5-minute update interval (configurable)
- **Integration type** - Hub (manages multiple devices/accounts)

## 🤝 Automation Examples

The integration supports powerful automations:
- Automatic bedtime device locking
- School day morning unlocks
- Screen time limit alerts
- Daily usage reports
- Conditional access based on homework completion

See the [README](https://github.com/noiwid/HAFamilySafety/blob/main/README.md) for complete automation examples.

## ⚖️ Important Notice

This integration uses **unofficial APIs** from Microsoft Family Safety. It is **not affiliated with, endorsed, or sponsored by Microsoft Corporation**.

- Microsoft may modify or restrict API access at any time
- Use at your own risk
- Comply with Microsoft's terms of service

## 🙏 Credits

- **[@pantherale0](https://github.com/pantherale0)** - [pyfamilysafety](https://github.com/pantherale0/pyfamilysafety) library
- Inspired by [HAFamilyLink](https://github.com/noiwid/HAFamilyLink)

## 📝 Full Changelog

Initial release with complete Microsoft Family Safety integration support.

---

**Install now:** [HACS Installation Guide](https://github.com/noiwid/HAFamilySafety/blob/main/INSTALL.md)

**Need help?** [Create an issue](https://github.com/noiwid/HAFamilySafety/issues)
