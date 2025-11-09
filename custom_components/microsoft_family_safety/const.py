"""Constants for the Microsoft Family Safety integration."""
from typing import Final

# Integration constants
DOMAIN: Final = "microsoft_family_safety"
INTEGRATION_NAME: Final = "Microsoft Family Safety"

# Configuration
CONF_TOKEN: Final = "token"
CONF_REDIRECT_URL: Final = "redirect_url"
CONF_REFRESH_TOKEN: Final = "refresh_token"
CONF_UPDATE_INTERVAL: Final = "update_interval"

# Defaults
DEFAULT_UPDATE_INTERVAL: Final = 300  # 5 minutes in seconds
DEFAULT_TIMEOUT: Final = 30

# Authentication URLs
MS_LOGIN_URL: Final = "https://login.live.com/oauth20_authorize.srf"
MS_AUTH_PARAMS: Final = {
    "cobrandid": "b5d15d4b-695a-4cd5-93c6-13f551b310df",
    "client_id": "000000000004893A",
    "response_type": "code",
    "redirect_uri": "https://login.live.com/oauth20_desktop.srf",
    "response_mode": "query",
    "scope": "service::familymobile.microsoft.com::MBI_SSL",
    "lw": "1",
    "fl": "easi2"
}

# API
API_TIMEOUT: Final = 30

# Error codes
ERROR_AUTH_FAILED: Final = "auth_failed"
ERROR_TIMEOUT: Final = "timeout"
ERROR_NETWORK: Final = "network_error"
ERROR_INVALID_DEVICE: Final = "invalid_device"
ERROR_TOKEN_EXPIRED: Final = "token_expired"

# Attributes
ATTR_DEVICE_ID: Final = "device_id"
ATTR_DEVICE_NAME: Final = "device_name"
ATTR_DEVICE_TYPE: Final = "device_type"
ATTR_LAST_SEEN: Final = "last_seen"
ATTR_BLOCKED: Final = "blocked"
ATTR_OS_NAME: Final = "os_name"
ATTR_DEVICE_MODEL: Final = "device_model"
ATTR_TODAY_TIME_USED: Final = "today_time_used"
ATTR_USER_ID: Final = "user_id"
ATTR_FIRST_NAME: Final = "first_name"
ATTR_SURNAME: Final = "surname"
ATTR_PROFILE_PICTURE: Final = "profile_picture"
ATTR_AVERAGE_SCREENTIME: Final = "average_screentime"
ATTR_ACCOUNT_BALANCE: Final = "account_balance"
ATTR_ACCOUNT_CURRENCY: Final = "account_currency"

# Platforms
PLATFORMS: Final = ["sensor"]
