# Microsoft Family Safety Integration for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/custom-components/hacs)
[![GitHub release](https://img.shields.io/github/release/noiwid/HAFamilySafety.svg)](https://github.com/noiwid/HAFamilySafety/releases)
[![License](https://img.shields.io/github/license/noiwid/HAFamilySafety.svg)](LICENSE)

A custom integration for Home Assistant to monitor and control Microsoft Family Safety accounts and devices.

## Features

- **Account Monitoring**
  - Daily screen time tracking
  - Average screen time statistics
  - Account balance monitoring (if available)
  - Profile information with profile pictures

- **Device Management**
  - Device screen time tracking
  - Device information (model, OS, last seen)
  - Block/unblock devices remotely
  - Real-time device status

- **Application Tracking**
  - Total application count per account
  - Blocked applications list
  - Application usage monitoring

- **Services**
  - Block/unblock devices
  - Approve/deny time extension requests
  - Custom automation support

## Installation

### HACS (Recommended)

1. Open HACS in Home Assistant
2. Click on "Integrations"
3. Click the three dots in the top right corner
4. Select "Custom repositories"
5. Add this repository URL: `https://github.com/noiwid/HAFamilySafety`
6. Select category: "Integration"
7. Click "Add"
8. Find "Microsoft Family Safety" in the integration list
9. Click "Download"
10. Restart Home Assistant

### Manual Installation

1. Copy the `custom_components/microsoft_family_safety` folder to your Home Assistant's `custom_components` directory
2. Restart Home Assistant

## Configuration

1. Go to Settings → Devices & Services
2. Click "+ Add Integration"
3. Search for "Microsoft Family Safety"
4. Follow the authentication steps:
   - Click on the provided Microsoft login URL
   - Log in with your Microsoft account
   - Copy the redirect URL from your browser
   - Extract the token (the value after `access_token=`)
   - Paste the token in Home Assistant

## Authentication

This integration uses the Microsoft Family Safety API which requires manual authentication:

1. You'll be provided with a special Microsoft login URL
2. After logging in, you'll be redirected to a blank page
3. The URL will contain an access token
4. Copy this token and paste it into the integration setup

**Security Note**: The token only grants access to Family Safety data, not other Microsoft services like OneDrive or Outlook.

## Entities

### Sensors

For each account:
- `sensor.<name>_screen_time` - Daily screen time in minutes
- `sensor.<name>_account_info` - Account information with profile picture
- `sensor.<name>_applications` - Application count and list
- `sensor.<name>_balance` - Account balance (if applicable)

For each device:
- `sensor.<device_name>_screen_time` - Device screen time
- `sensor.<device_name>_info` - Device information

### Switches

For each device:
- `switch.<device_name>` - Control device blocking
  - ON = Device is unblocked (active)
  - OFF = Device is blocked

## Services

### `microsoft_family_safety.block_device`

Block a device from being used.

```yaml
service: microsoft_family_safety.block_device
data:
  device_id: "device_id_here"
  duration: 60  # Optional: duration in minutes (omit for indefinite)
```

### `microsoft_family_safety.unblock_device`

Unblock a previously blocked device.

```yaml
service: microsoft_family_safety.unblock_device
data:
  device_id: "device_id_here"
```

### `microsoft_family_safety.approve_request`

Approve a pending time extension request.

```yaml
service: microsoft_family_safety.approve_request
data:
  request_id: "request_id_here"
  extension_time: 30  # Minutes to grant
```

### `microsoft_family_safety.deny_request`

Deny a pending time extension request.

```yaml
service: microsoft_family_safety.deny_request
data:
  request_id: "request_id_here"
```

## Example Automations

### Bedtime Device Lock

```yaml
automation:
  - alias: "Lock kids devices at bedtime"
    trigger:
      - platform: time
        at: "21:00:00"
    action:
      - service: switch.turn_off
        target:
          entity_id: switch.kids_phone
```

### Screen Time Alert

```yaml
automation:
  - alias: "Alert when screen time exceeds 2 hours"
    trigger:
      - platform: numeric_state
        entity_id: sensor.child_screen_time
        above: 120
    action:
      - service: notify.mobile_app
        data:
          message: "Child has exceeded 2 hours of screen time today"
```

## Troubleshooting

### Token Expired

If you see authentication errors, your token may have expired. Go to the integration settings and reconfigure with a new token.

### No Data Showing

- Ensure you have Family Safety accounts set up in your Microsoft account
- Check that devices are enrolled in Family Safety
- Wait a few minutes for the initial data fetch

### Devices Not Responding

- Verify the device is online and connected to the internet
- Check that Family Safety is enabled on the device
- Try refreshing the integration

## Credits

- Based on [pyfamilysafety](https://github.com/pantherale0/pyfamilysafety) by @pantherale0
- Inspired by [HAFamilyLink](https://github.com/noiwid/HAFamilyLink)

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Support

For issues, questions, or contributions, please visit the [GitHub repository](https://github.com/noiwid/HAFamilySafety/issues).
