# Microsoft Family Safety - Installation Guide

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=noiwid&repository=HAFamilySafety&category=Integration)

This guide will walk you through installing and configuring the Microsoft Family Safety integration for Home Assistant.

## Table of Contents

- [Prerequisites](#prerequisites)
- [Installation](#installation)
  - [Method 1: HACS (Recommended)](#method-1-hacs-recommended)
  - [Method 2: Manual Installation](#method-2-manual-installation)
- [Configuration](#configuration)
  - [Step-by-Step Setup](#step-by-step-setup)
  - [Authentication Process](#authentication-process)
- [Verification](#verification)
- [Troubleshooting](#troubleshooting)

---

## Prerequisites

Before installing this integration, ensure you have:

1. **Home Assistant** installed and running (version 2024.1.0 or higher recommended)
2. **HACS** installed (for recommended installation method) - [HACS Installation Guide](https://hacs.xyz/docs/setup/download)
3. **Microsoft Family Safety account** configured with:
   - At least one child account added to your family
   - At least one device registered and monitored in Microsoft Family Safety
   - Active supervision enabled on child devices
4. **Internet connection** - Required for API communication with Microsoft servers
5. **Access to your Microsoft account credentials** (parent/organizer account)

> **Note:** To verify your Family Safety setup, visit [Microsoft Family Safety](https://account.microsoft.com/family) and ensure you can see your children's accounts and their devices.

---

## Installation

### Method 1: HACS (Recommended)

HACS (Home Assistant Community Store) makes installation and updates easier.

#### Step 1: Add Custom Repository

1. Open **HACS** in your Home Assistant interface
2. Click on **"Integrations"**
3. Click the **three-dot menu** (⋮) in the top-right corner
4. Select **"Custom repositories"**
5. In the dialog that appears:
   - **Repository URL:** `https://github.com/noiwid/HAFamilySafety`
   - **Category:** Select `Integration`
6. Click **"Add"**

#### Step 2: Install the Integration

1. Still in HACS Integrations, click the **"+ Explore & Download Repositories"** button
2. Search for **"Microsoft Family Safety"**
3. Click on the **Microsoft Family Safety** integration
4. Click **"Download"**
5. Select the latest version and click **"Download"** again
6. **Restart Home Assistant**
   - Go to **Settings** → **System** → **Restart**
   - Wait for Home Assistant to fully restart (1-2 minutes)

---

### Method 2: Manual Installation

If you prefer not to use HACS or don't have it installed:

#### Step 1: Download the Integration

1. Download the latest release from [GitHub Releases](https://github.com/noiwid/HAFamilySafety/releases)
2. Extract the downloaded ZIP file

#### Step 2: Copy Files

1. Navigate to your Home Assistant configuration directory (usually `/config`)
2. If it doesn't exist, create a `custom_components` folder
3. Copy the entire `microsoft_family_safety` folder from the extracted files into `/config/custom_components/`

   Your directory structure should look like:
   ```
   /config/
   └── custom_components/
       └── microsoft_family_safety/
           ├── __init__.py
           ├── config_flow.py
           ├── coordinator.py
           ├── sensor.py
           ├── switch.py
           ├── const.py
           ├── manifest.json
           ├── strings.json
           └── translations/
   ```

#### Step 3: Restart Home Assistant

1. Go to **Settings** → **System** → **Restart**
2. Wait for Home Assistant to fully restart

---

## Configuration

### Step-by-Step Setup

Now that the integration is installed, you need to configure it with your Microsoft account.

#### Step 1: Add Integration

1. In Home Assistant, go to **Settings** → **Devices & Services**
2. Click the **"+ Add Integration"** button in the bottom-right corner
3. Search for **"Microsoft Family Safety"**
4. Click on **Microsoft Family Safety** in the results

#### Step 2: Initial Authentication Screen

You'll see a configuration dialog with:
- An authentication URL
- Instructions to proceed

1. **Copy the authentication URL** provided in the dialog
2. Click **"Next"** to proceed to the next step

> **Important:** Keep the Home Assistant configuration window open - you'll need to return to it shortly.

---

### Authentication Process

This is the most important part of the setup. Follow these steps carefully.

#### Step 3: Microsoft Login

1. **Open a new browser tab or window**
2. **Paste the authentication URL** you copied earlier
   - The URL will look like: `https://login.live.com/oauth20_authorize.srf?cobrandid=...`
3. **Log in with your Microsoft account**
   - Use the **parent/organizer account** (not a child account)
   - This is the account that manages your Family Safety settings
4. **Grant permissions** if prompted
   - Microsoft will ask you to confirm access to Family Safety data
   - Click **"Yes"** or **"Accept"** to continue

#### Step 4: Capture the Redirect URL

After logging in successfully, Microsoft will redirect you to a **blank page** or a page with a Microsoft error message. **This is normal and expected!**

What's important is the **URL in your browser's address bar**.

1. **Look at the URL** in your browser's address bar
2. It should look something like this:
   ```
   https://login.live.com/oauth20_desktop.srf?code=M.C123_BAY.0.U.-AbCdEfGhIjKlMnOpQrStUvWxYz1234567890&lc=1033
   ```
3. **Copy the entire URL** from the address bar
   - Make sure you copy everything, including `https://` at the start
   - The URL contains a `code=` parameter - this is the authorization code needed

> **Visual Guide:**
> - Browser shows: Blank page or "This site can't be reached"
> - Address bar contains: Long URL starting with `https://login.live.com/oauth20_desktop.srf?code=...`
> - Action: Copy the **entire URL** from the address bar

#### Step 5: Complete Configuration in Home Assistant

1. **Return to the Home Assistant configuration window** you left open
2. You should now see a field labeled **"Redirect URL"**
3. **Paste the complete URL** you just copied from your browser
4. Click **"Submit"**

#### Step 6: Configuration Complete

If everything is correct:
- Home Assistant will validate the authentication
- The integration will create a refresh token for future API calls
- You'll see a success message
- The Microsoft Family Safety integration will appear in your **Devices & Services** page

---

## Verification

### Check Integration Status

1. Go to **Settings** → **Devices & Services**
2. Find **Microsoft Family Safety** in the integrations list
3. Click on it to see:
   - Number of devices
   - Number of entities created
   - Configuration options

### Verify Entities Created

1. Go to **Settings** → **Devices & Services** → **Entities**
2. Search for entities containing your child's name
3. You should see entities like:
   - **Sensors:**
     - `sensor.<child_name>_screen_time` - Daily screen time in minutes
     - `sensor.<child_name>_account_info` - Account information with profile picture
     - `sensor.<child_name>_applications` - Number of applications
     - `sensor.<device_name>_screen_time` - Per-device screen time
     - `sensor.<device_name>_info` - Device information
   - **Switches:**
     - `switch.<device_name>` - Device block/unblock control

### First Data Update

The integration updates data every **5 minutes** by default. If you don't see data immediately:
- Wait 5 minutes for the first update
- Click on a sensor to see if it's updating
- Check the Home Assistant logs for any errors

---

## Troubleshooting

### Authentication Failed

**Problem:** Error message "Authentication failed" or "Invalid redirect URL"

**Solutions:**
1. **Check the URL is complete**
   - Make sure you copied the entire URL from the browser
   - The URL must include the `code=` parameter
   - Don't copy just the code - copy the whole URL

2. **Try again immediately**
   - Authorization codes expire quickly (within minutes)
   - If you waited too long between getting the URL and pasting it, start over

3. **Use the correct account**
   - Log in with the **parent/organizer account**
   - Don't use a child account for authentication

4. **Check browser issues**
   - Try using incognito/private browsing mode
   - Clear browser cache and cookies
   - Try a different browser

### No Entities Created

**Problem:** Integration installed but no sensors or switches appear

**Solutions:**
1. **Verify Family Safety setup**
   - Log in to [Microsoft Family Safety](https://account.microsoft.com/family)
   - Confirm at least one child account exists
   - Confirm at least one device is registered and active
   - Check that devices are online and have recent activity

2. **Wait for first update**
   - The integration updates every 5 minutes
   - Wait at least 5-10 minutes after initial setup

3. **Check logs**
   - Go to **Settings** → **System** → **Logs**
   - Search for `microsoft_family_safety`
   - Look for error messages

4. **Restart the integration**
   - Go to **Settings** → **Devices & Services**
   - Find **Microsoft Family Safety**
   - Click the three-dot menu (⋮) → **Reload**

### Token Expired / Re-authentication Required

**Problem:** Integration stops working after some time, logs show authentication errors

**Solution:**
1. Go to **Settings** → **Devices & Services**
2. Find **Microsoft Family Safety**
3. Click **"Configure"** or the three-dot menu (⋮) → **"Reconfigure"**
4. Follow the authentication process again (Steps 3-5 in [Authentication Process](#authentication-process))
5. The integration will update with a new refresh token

### Devices Don't Respond to Commands

**Problem:** Using the switch to block/unblock devices doesn't work

**Solutions:**
1. **Check device is online**
   - Device must be connected to the Internet
   - Check device status in Microsoft Family Safety app

2. **Verify Family Safety is active**
   - Ensure Family Safety app/service is running on the device
   - Check device isn't in "local management only" mode

3. **Test from Microsoft Family Safety**
   - Try blocking/unblocking from the official Microsoft Family Safety app/website
   - If it doesn't work there, it's a Microsoft account/device configuration issue

4. **Check API delays**
   - Commands can take 1-2 minutes to take effect
   - Device must check in with Microsoft servers

### Enable Debug Logging

For detailed troubleshooting, enable debug logs:

1. Edit your `configuration.yaml` file:
   ```yaml
   logger:
     default: info
     logs:
       custom_components.microsoft_family_safety: debug
       pyfamilysafety: debug
   ```

2. **Restart Home Assistant**

3. **Reproduce the issue**

4. **Check logs:**
   - Go to **Settings** → **System** → **Logs**
   - Look for detailed error messages
   - Include these logs when reporting issues

### Missing Dependencies Error

**Problem:** Error about `pyfamilysafety` not found

**Solutions:**
1. **Restart Home Assistant completely**
   - Go to **Settings** → **System** → **Restart**
   - Wait for full restart

2. **Check Home Assistant version**
   - This integration requires Home Assistant 2024.1.0 or higher
   - Update Home Assistant if needed

3. **Reinstall integration**
   - Remove the integration
   - Delete the `custom_components/microsoft_family_safety` folder
   - Reinstall via HACS or manually

### Getting Help

If you're still having issues:

1. **Check existing issues:** [GitHub Issues](https://github.com/noiwid/HAFamilySafety/issues)
2. **Create a new issue** with:
   - Home Assistant version
   - Integration version
   - Detailed description of the problem
   - Relevant log entries (with debug enabled)
   - Steps to reproduce
3. **Community support:** [Home Assistant Community Forum](https://community.home-assistant.io/)

---

## Next Steps

Once installed and configured successfully:

1. **Explore the entities** - Check all sensors and switches created
2. **Create automations** - Set up bedtime device blocking, screen time alerts, etc.
3. **Build dashboards** - Display screen time data and device controls
4. **Read the README** - Learn about all available features and services

For detailed information about features, services, and automation examples, see the [README.md](https://github.com/noiwid/HAFamilySafety/blob/main/README.md).

---

## Security & Privacy

- **Authentication:** Secure OAuth 2.0 flow with Microsoft
- **Token storage:** Refresh tokens stored securely in Home Assistant's configuration
- **Permissions:** Access only to Microsoft Family Safety data (no OneDrive, Outlook, etc.)
- **API usage:** Unofficial Microsoft Family Safety API - use at your own risk
- **Data:** All data fetched from Microsoft servers; only refresh token stored locally

---

**Installation complete!** You're now ready to monitor and control Microsoft Family Safety devices from Home Assistant.
