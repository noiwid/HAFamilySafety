"""Client to read cookies from Family Safety Auth add-on or standalone container."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import aiohttp
from cryptography.fernet import Fernet

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Default URL for local add-on (Home Assistant OS/Supervised)
DEFAULT_AUTH_URL = "http://localhost:8098"


class AddonCookieClient:
    """Client to read cookies from add-on via API or shared storage."""

    SHARE_DIR = Path("/share/familysafety")
    COOKIE_FILE = "cookies.enc"
    KEY_FILE = ".key"

    def __init__(self, hass: HomeAssistant, auth_url: str | None = None):
        """Initialize addon cookie client."""
        self.hass = hass
        self.auth_url = auth_url
        self.storage_path = self.SHARE_DIR / self.COOKIE_FILE
        self.key_file = self.SHARE_DIR / self.KEY_FILE
        self._detected_url: str | None = None

    async def _fetch_cookies_from_url(self, url: str) -> list[dict[str, Any]] | None:
        """Fetch cookies from auth server API."""
        api_url = f"{url.rstrip('/')}/api/cookies"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    api_url, timeout=aiohttp.ClientTimeout(total=10)
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        cookies = data.get("cookies", [])
                        _LOGGER.info("Loaded %d cookies from API (%s)", len(cookies), url)
                        return cookies
                    if response.status == 404:
                        _LOGGER.debug("No cookies found at %s", api_url)
                        return None
                    _LOGGER.debug("API returned status %s from %s", response.status, api_url)
                    return None
        except aiohttp.ClientError as err:
            _LOGGER.debug("Failed to connect to %s: %s", api_url, err)
            return None
        except Exception as err:
            _LOGGER.debug("Error fetching cookies from %s: %s", api_url, err)
            return None

    async def _check_url_available(self, url: str) -> bool:
        """Check if auth server API is available at URL."""
        health_url = f"{url.rstrip('/')}/api/health"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    health_url, timeout=aiohttp.ClientTimeout(total=5)
                ) as response:
                    return response.status == 200
        except Exception:
            return False

    async def _get_encryption_key(self) -> bytes:
        """Get encryption key (must match add-on key)."""
        if not await self.hass.async_add_executor_job(self.key_file.exists):
            raise FileNotFoundError(
                "Encryption key not found. Make sure the Family Safety Auth "
                "add-on is installed and has been used at least once."
            )
        return await self.hass.async_add_executor_job(self.key_file.read_bytes)

    async def _load_cookies_from_file(self) -> list[dict[str, Any]] | None:
        """Load cookies from encrypted file (fallback mode)."""
        if not await self.hass.async_add_executor_job(self.storage_path.exists):
            _LOGGER.debug("No cookies found in shared storage")
            return None

        try:
            encrypted = await self.hass.async_add_executor_job(
                self.storage_path.read_bytes
            )
            key = await self._get_encryption_key()
            fernet = Fernet(key)
            decrypted = fernet.decrypt(encrypted)

            data = json.loads(decrypted.decode())
            cookies = data.get("cookies", [])

            _LOGGER.info("Loaded %d cookies from file", len(cookies))
            return cookies

        except Exception as err:
            _LOGGER.error("Failed to load cookies from file: %s", err)
            return None

    async def _file_available(self) -> bool:
        """Check if cookie file is available."""
        storage_exists = await self.hass.async_add_executor_job(
            self.storage_path.exists
        )
        key_exists = await self.hass.async_add_executor_job(self.key_file.exists)
        return storage_exists and key_exists

    async def detect_auth_source(self) -> tuple[str, str | None]:
        """Detect available authentication source.

        Returns:
            Tuple of (source_type, url_or_none):
            - ("api", "http://...") if API is available
            - ("file", None) if file is available
            - ("none", None) if nothing is available
        """
        # 1. If custom URL is configured, check it first
        if self.auth_url:
            if await self._check_url_available(self.auth_url):
                self._detected_url = self.auth_url
                return ("api", self.auth_url)

        # 2. Try default local URL (add-on)
        if await self._check_url_available(DEFAULT_AUTH_URL):
            self._detected_url = DEFAULT_AUTH_URL
            return ("api", DEFAULT_AUTH_URL)

        # 3. Fallback to file
        if await self._file_available():
            return ("file", None)

        # 4. Nothing available
        return ("none", None)

    async def load_cookies(self) -> list[dict[str, Any]] | None:
        """Load cookies using best available method."""
        # 1. If custom URL is configured, use it
        if self.auth_url:
            cookies = await self._fetch_cookies_from_url(self.auth_url)
            if cookies is not None:
                return cookies
            _LOGGER.warning(
                "Failed to load cookies from configured URL: %s", self.auth_url
            )

        # 2. Try default local API
        cookies = await self._fetch_cookies_from_url(DEFAULT_AUTH_URL)
        if cookies is not None:
            return cookies

        # 3. Fallback to file
        _LOGGER.debug("API not available, trying file fallback")
        return await self._load_cookies_from_file()

    async def cookies_available(self) -> bool:
        """Check if cookies are available from any source."""
        source_type, _ = await self.detect_auth_source()
        if source_type == "none":
            return False

        cookies = await self.load_cookies()
        return cookies is not None and len(cookies) > 0

    async def fetch_screentime(self, child_id: str) -> dict | None:
        """Fetch screen time policy via the addon's browser-based endpoint.

        The addon launches a headless browser with saved cookies and calls
        Microsoft's API via fetch() from within the authenticated page context.
        This avoids the 401 errors that occur when replaying cookies with aiohttp.
        """
        url = self._detected_url or self.auth_url or DEFAULT_AUTH_URL
        api_url = f"{url.rstrip('/')}/api/screentime"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    api_url,
                    params={"childId": child_id},
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        result = data.get("data")
                        _LOGGER.info(
                            "Screen time fetched via addon browser for child %s",
                            child_id,
                        )
                        return result
                    text = await response.text()
                    # Try to parse detailed error info from addon
                    detail = text
                    try:
                        import json
                        err_data = json.loads(text)
                        if isinstance(err_data, dict):
                            err_detail = err_data.get("detail", {})
                            if isinstance(err_detail, dict):
                                ms_status = err_detail.get("microsoft_status", "?")
                                error_code = err_detail.get("error", "?")
                                message = err_detail.get("message", "")[:300]
                                detail = (
                                    f"code={error_code} microsoft_status={ms_status} "
                                    f"message={message}"
                                )
                    except Exception:
                        pass
                    _LOGGER.warning(
                        "Addon screentime API returned %s: %s",
                        response.status,
                        detail[:500],
                    )
                    return None
        except aiohttp.ClientError as err:
            _LOGGER.warning("Failed to fetch screentime from addon: %s", err)
            return None
        except Exception as err:
            _LOGGER.error("Unexpected error fetching screentime: %s", err)
            return None
