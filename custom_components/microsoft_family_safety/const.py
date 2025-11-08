"""Constants for the Microsoft Family Safety integration."""
from typing import Final

# Integration constants
DOMAIN: Final = "microsoft_family_safety"
INTEGRATION_NAME: Final = "Microsoft Family Safety"

# Configuration
CONF_TOKEN: Final = "token"
CONF_UPDATE_INTERVAL: Final = "update_interval"

# Defaults
DEFAULT_UPDATE_INTERVAL: Final = 300  # 5 minutes in seconds
DEFAULT_TIMEOUT: Final = 30

# Authentication URLs
MS_LOGIN_URL: Final = "https://login.live.com/oauth20_authorize.srf"
MS_AUTH_PARAMS: Final = {
    "cobrandid": "8058f65d-ce06-4c30-9559-473c9275a65d",
    "client_id": "000000000004893A",
    "response_type": "token",
    "scope": "service::family.microsoft.com::MBI_SSL",
    "redirect_uri": "https://login.live.com/oauth20_desktop.srf"
}

# API
API_TIMEOUT: Final = 30

# Device control actions
DEVICE_BLOCK_ACTION: Final = "block"
DEVICE_UNBLOCK_ACTION: Final = "unblock"

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

# Services
SERVICE_BLOCK_DEVICE: Final = "block_device"
SERVICE_UNBLOCK_DEVICE: Final = "unblock_device"
SERVICE_APPROVE_REQUEST: Final = "approve_request"
SERVICE_DENY_REQUEST: Final = "deny_request"

# Platforms
PLATFORMS: Final = ["sensor", "switch"]
