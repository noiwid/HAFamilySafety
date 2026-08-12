"""Config flow for Microsoft Family Safety integration."""
from __future__ import annotations

import logging
from ipaddress import ip_address
from typing import Any
from urllib.parse import unquote

import voluptuous as vol
from yarl import URL

from homeassistant import config_entries
from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_RECONFIGURE
import homeassistant.helpers.config_validation as cv
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.network import NoURLAvailableError, get_url

from .const import (
    AVAILABLE_PLATFORMS,
    CONF_ALLOW_INSECURE_HTTP_AUTH,
    CONF_API_KEY,
    CONF_AUTH_USER_ID,
    CONF_AUTH_URL,
    CONF_PLATFORMS,
    CONF_REDIRECT_URL,
    CONF_REFRESH_TOKEN,
    CONF_UPDATE_INTERVAL,
    CONF_WEB_COOKIES,
    CONF_WEB_FAMILY_REFERER,
    CONF_WEB_FAMILY_TOKEN,
    DEFAULT_PLATFORMS,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    ERROR_AUTH_FAILED,
    INTEGRATION_NAME,
    MS_AUTH_PARAMS,
    MS_LOGIN_URL,
)

_LOGGER = logging.getLogger(__name__)


def _build_auth_url() -> str:
    """Build the Microsoft Family Safety mobile OAuth URL."""
    return str(URL(MS_LOGIN_URL).with_query(MS_AUTH_PARAMS))


async def validate_redirect_url(hass: HomeAssistant, redirect_url: str) -> dict[str, Any]:
    """Exchange the intercepted Microsoft OAuth redirect for a refresh token."""
    try:
        from ._pyfamilysafety_compat import create_authenticator

        redirect_url = unquote(redirect_url)
        parsed_redirect = URL(redirect_url)
        _LOGGER.debug(
            "Validating captured Microsoft Family OAuth redirect: host=%s path=%s "
            "has_code=%s has_error=%s query_keys=%s",
            parsed_redirect.host,
            parsed_redirect.path,
            "code" in parsed_redirect.query,
            "error" in parsed_redirect.query,
            sorted(parsed_redirect.query.keys()),
        )
        authenticator = await create_authenticator(
            hass,
            redirect_url,
            use_refresh_token=False,
        )
        if not authenticator.refresh_token:
            _LOGGER.warning(
                "Microsoft Family OAuth redirect validated but no refresh token was returned"
            )
            raise InvalidAuth("Microsoft did not return a refresh token")
        _LOGGER.debug(
            "Microsoft Family OAuth redirect validation succeeded: user_id_present=%s "
            "refresh_token_present=%s",
            bool(authenticator.user_id),
            bool(authenticator.refresh_token),
        )
        return {
            "title": INTEGRATION_NAME,
            "refresh_token": authenticator.refresh_token,
            "user_id": str(authenticator.user_id or ""),
        }
    except InvalidAuth:
        raise
    except Exception as err:
        _LOGGER.error("Authentication validation failed: %s", err)
        raise InvalidAuth(f"Cannot connect: {err}") from err


class FamilySafetyConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Microsoft Family Safety."""

    VERSION = 1

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> FamilySafetyOptionsFlow:
        """Get the options flow for this handler."""
        return FamilySafetyOptionsFlow(config_entry)

    def __init__(self) -> None:
        self._detected_source: str | None = None
        self._detected_url: str | None = None
        self._pending_data: dict[str, Any] | None = None
        self._pending_options: dict[str, Any] | None = None
        self._pending_title: str = INTEGRATION_NAME
        self._native_proxy: Any | None = None
        self._mobile_proxy: Any | None = None
        self._mobile_sso_cookies: list[dict[str, Any]] = []
        self._pending_auth_url: str | None = None
        self._pending_api_key: str | None = None
        self._native_mobile_abort_reason: str | None = None
        self._allow_insecure_http_auth: bool = False
        self._family_wait_task: Any | None = None

    def _is_existing_entry_auth_flow(self) -> bool:
        """Return whether this flow updates an existing config entry."""
        return self.source in (SOURCE_REAUTH, SOURCE_RECONFIGURE)

    def _get_existing_entry(self):
        """Return the config entry targeted by reauth or reconfigure."""
        if self.source == SOURCE_REAUTH:
            return self._get_reauth_entry()
        if self.source == SOURCE_RECONFIGURE:
            return self._get_reconfigure_entry()
        raise HomeAssistantError(
            f"No existing config entry is associated with {self.source!r} flow"
        )

    @staticmethod
    def _is_local_http_url(url: URL) -> bool:
        """Return True only for HTTP URLs that are clearly local/private."""
        if url.scheme != "http" or not url.host:
            return False
        host = url.host.lower().rstrip(".")
        if host in {"localhost", "homeassistant", "homeassistant.local"} or host.endswith(".local"):
            return True
        try:
            address = ip_address(host)
        except ValueError:
            return False
        return address.is_private or address.is_loopback or address.is_link_local

    def _browser_hass_url(self) -> str:
        """Return a browser-reachable HA URL for the temporary auth proxy.

        HTTPS is always preferred. Plain HTTP is permitted only when the user
        explicitly opted in and the URL is clearly local/private.
        """
        candidates: list[str] = []
        for kwargs in (
            {"prefer_external": True},
            {"allow_external": False},
        ):
            try:
                candidate = get_url(self.hass, **kwargs)
            except NoURLAvailableError:
                continue
            if candidate not in candidates:
                candidates.append(candidate)

        for candidate in candidates:
            if URL(candidate).scheme == "https":
                return candidate

        if self._allow_insecure_http_auth:
            for candidate in reversed(candidates):
                url = URL(candidate)
                if self._is_local_http_url(url):
                    _LOGGER.warning(
                        "Microsoft Family Safety authentication is using an "
                        "UNENCRYPTED local Home Assistant HTTP proxy at %s. "
                        "Microsoft credentials and session data are not protected "
                        "against interception on the local network.",
                        url.host,
                    )
                    return candidate
            raise NativeAuthLocalHttpRequired

        raise NativeAuthHttpsRequired

    async def _detect_legacy_auth(self) -> None:
        """Detect an optional legacy Playwright auth source without blocking setup."""
        try:
            from .auth.addon_client import AddonCookieClient

            source_type, detected_url = await AddonCookieClient(self.hass).detect_auth_source()
        except Exception as err:
            _LOGGER.debug("Legacy Family Safety auth source detection failed: %s", err)
            source_type, detected_url = "none", None
        self._detected_source = source_type
        self._detected_url = detected_url

    def _user_schema(self, user_input: dict[str, Any] | None = None) -> vol.Schema:
        """Build the initial setup schema."""
        values = user_input or {}
        return vol.Schema(
            {
                vol.Optional(
                    CONF_UPDATE_INTERVAL,
                    default=values.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL),
                ): vol.All(vol.Coerce(int), vol.Range(min=30, max=3600)),
                vol.Optional(
                    CONF_PLATFORMS,
                    default=values.get(CONF_PLATFORMS, DEFAULT_PLATFORMS),
                ): cv.multi_select({p: p for p in AVAILABLE_PLATFORMS}),
                vol.Optional(
                    CONF_ALLOW_INSECURE_HTTP_AUTH,
                    default=values.get(CONF_ALLOW_INSECURE_HTTP_AUTH, False),
                ): cv.boolean,
                vol.Optional(CONF_AUTH_URL, default=values.get(CONF_AUTH_URL, "")): str,
                vol.Optional(CONF_API_KEY, default=values.get(CONF_API_KEY, "")): str,
            }
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Collect basic options, then start browser-assisted Microsoft OAuth."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._allow_insecure_http_auth = bool(
                user_input.get(CONF_ALLOW_INSECURE_HTTP_AUTH, False)
            )
            self._pending_options = {
                CONF_UPDATE_INTERVAL: user_input.get(
                    CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL
                ),
                CONF_PLATFORMS: user_input.get(CONF_PLATFORMS, DEFAULT_PLATFORMS),
                CONF_ALLOW_INSECURE_HTTP_AUTH: self._allow_insecure_http_auth,
            }
            self._pending_auth_url = user_input.get(CONF_AUTH_URL, "").strip() or None
            self._pending_api_key = user_input.get(CONF_API_KEY, "").strip() or None
            await self._detect_legacy_auth()
            try:
                return await self.async_step_start_mobile_auth()
            except NativeAuthLocalHttpRequired:
                errors["base"] = "native_http_not_local"
                return self.async_show_form(
                    step_id="user",
                    data_schema=self._user_schema(user_input),
                    description_placeholders={
                        "auth_url": "automatically handled by Home Assistant",
                        "addon_status": "native Home Assistant browser authentication",
                    },
                    errors=errors,
                )
            except NativeAuthHttpsRequired:
                # A native entry is only complete when both mobile OAuth and the
                # account.microsoft.com web session can be captured. Keep manual
                # mobile OAuth only for explicitly configured legacy add-on users.
                if self._detected_source in ("api", "file") or self._pending_auth_url:
                    return await self.async_step_auth()
                errors["base"] = "native_https_required"
                return self.async_show_form(
                    step_id="user",
                    data_schema=self._user_schema(user_input),
                    description_placeholders={
                        "auth_url": "automatically handled by Home Assistant",
                        "addon_status": "native Home Assistant browser authentication",
                    },
                    errors=errors,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=self._user_schema(user_input),
            # Compatibility placeholders for clients that still have the pre-v5
            # translation cached. The current v5+ text does not reference them.
            description_placeholders={
                "auth_url": "automatically handled by Home Assistant",
                "addon_status": "native Home Assistant browser authentication",
            },
            errors=errors,
        )

    async def async_step_start_mobile_auth(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Start the Home Assistant hosted proxy for mobile OAuth."""
        hass_url = URL(self._browser_hass_url())
        try:
            from .auth.native_proxy import (
                AUTH_CALLBACK_PATH,
                MicrosoftFamilyAuthProxy,
                register_native_proxy,
            )
        except Exception as err:
            _LOGGER.exception("Native Family Safety auth module could not be loaded")
            return self.async_abort(
                reason="native_auth_unavailable",
                description_placeholders={"error": str(err)},
            )

        callback_url = str(
            hass_url.with_path(AUTH_CALLBACK_PATH).with_query({"flow_id": self.flow_id})
        )
        self._mobile_proxy = MicrosoftFamilyAuthProxy(
            self.hass,
            callback_url=callback_url,
            start_url=_build_auth_url(),
            completion_mode="oauth",
        )
        register_native_proxy(self.hass, self._mobile_proxy)
        proxy_url = str(hass_url.with_path(self._mobile_proxy.access_path))
        _LOGGER.debug("Starting native Microsoft Family mobile OAuth proxy")
        return self.async_external_step(step_id="check_mobile_proxy", url=proxy_url)

    async def async_step_check_mobile_proxy(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Complete mobile OAuth and continue directly into web SSO.

        For native authentication the mobile OAuth and account.microsoft.com
        web-session phases are one browser journey.  Returning the web external
        step directly lets the native callback redirect the already-open tab to
        the second phase.  This avoids requiring a second popup / second press of
        Home Assistant's generic ``Open website`` button.

        Legacy add-on entries keep the older EXTERNAL_STEP_DONE path because
        they do not require the native account.microsoft.com web session.
        """
        # v37 deliberately keeps *both* Microsoft phases on the same HA external
        # step id.  Home Assistant's frontend GET for an external-step progress
        # event calls async_configure(), not a passive getter.  If the mobile->web
        # transition used a second step id, that GET could prematurely execute the
        # web completion step before Microsoft had finished SSO and abort the flow.
        # The already-open browser tab is redirected directly by the callback, so
        # the frontend does not need a second external-step transition at all.
        web_proxy = self._native_proxy
        if web_proxy is not None:
            _LOGGER.debug(
                "Native auth flow %s handling web phase on shared external step: "
                "complete=%s family_wait_started=%s pending_data=%s cookies=%d",
                self.flow_id,
                web_proxy.complete,
                bool(getattr(web_proxy, "family_wait_started", False)),
                self._pending_data is not None,
                len(web_proxy.export_cookies()),
            )
            # Once the visible Microsoft account sign-in has completed, move the
            # Home Assistant dialog from the generic EXTERNAL step to HA's native
            # SHOW_PROGRESS step.  The browser tab stays open and continues the
            # Family silent-SSO bootstrap in parallel.
            if getattr(web_proxy, "family_wait_started", False):
                _LOGGER.debug(
                    "Native auth flow %s Family SSO wait started; returning "
                    "external_done -> wait_family_proxy",
                    self.flow_id,
                )
                return self.async_external_step_done(next_step_id="wait_family_proxy")
            return self.async_external_step(
                step_id="check_mobile_proxy",
                url=str(
                    URL(self._browser_hass_url()).with_path(web_proxy.access_path)
                ),
            )

        proxy = self._mobile_proxy
        _LOGGER.debug(
            "Native auth flow %s handling mobile phase: proxy_present=%s "
            "complete=%s redirect_present=%s",
            self.flow_id,
            proxy is not None,
            bool(proxy and proxy.complete),
            bool(proxy and proxy.oauth_redirect_url),
        )
        if proxy is None or not proxy.complete or not proxy.oauth_redirect_url:
            if proxy is not None:
                return self.async_external_step(
                    step_id="check_mobile_proxy",
                    url=str(
                        URL(self._browser_hass_url()).with_path(proxy.access_path)
                    ),
                )
            return self.async_external_step_done(next_step_id="finish_mobile_proxy")

        # Preserve the legacy add-on flow unchanged.  The native flow below is
        # deliberately restricted to entries that need a Family web session.
        if self._is_existing_entry_auth_flow():
            entry = self._get_existing_entry()
            legacy_addon = bool(entry.data.get(CONF_AUTH_URL)) and not entry.data.get(
                CONF_WEB_COOKIES
            )
            if legacy_addon:
                return self.async_external_step_done(
                    next_step_id="finish_mobile_proxy"
                )
        elif self._detected_source in ("api", "file") or self._pending_auth_url:
            return self.async_external_step_done(next_step_id="finish_mobile_proxy")

        redirect_url = proxy.oauth_redirect_url
        self._mobile_sso_cookies = proxy.export_cookies()
        _LOGGER.debug(
            "Native auth flow %s captured mobile OAuth completion: "
            "sso_cookie_count=%d",
            self.flow_id,
            len(self._mobile_sso_cookies),
        )
        try:
            from .auth.native_proxy import unregister_native_proxy

            await unregister_native_proxy(self.hass, proxy)
        except Exception as err:
            _LOGGER.debug("Could not close mobile OAuth proxy cleanly: %s", err)
        self._mobile_proxy = None

        try:
            info = await validate_redirect_url(self.hass, redirect_url)
        except InvalidAuth as err:
            _LOGGER.warning(
                "Native auth flow %s mobile OAuth validation failed (%s)",
                self.flow_id,
                type(err).__name__,
            )
            self._native_mobile_abort_reason = "native_oauth_failed"
            return self.async_external_step_done(next_step_id="finish_mobile_proxy")

        refresh_token = info["refresh_token"]
        new_user_id = str(info.get("user_id") or "")

        if self._is_existing_entry_auth_flow():
            entry = self._get_existing_entry()
            old_user_id = str(entry.data.get(CONF_AUTH_USER_ID, ""))
            if old_user_id and new_user_id and old_user_id != new_user_id:
                self._native_mobile_abort_reason = "wrong_account"
                return self.async_external_step_done(
                    next_step_id="finish_mobile_proxy"
                )

            new_data: dict[str, Any] = {CONF_REFRESH_TOKEN: refresh_token}
            if new_user_id:
                new_data[CONF_AUTH_USER_ID] = new_user_id
            if CONF_AUTH_URL in entry.data:
                new_data[CONF_AUTH_URL] = entry.data[CONF_AUTH_URL]
            if CONF_API_KEY in entry.data:
                new_data[CONF_API_KEY] = entry.data[CONF_API_KEY]

            self._pending_data = new_data
            merged_options = dict(entry.options)
            if self._pending_options:
                merged_options.update(self._pending_options)
            self._pending_options = merged_options
            self._allow_insecure_http_auth = bool(
                merged_options.get(CONF_ALLOW_INSECURE_HTTP_AUTH, False)
            )
            self._pending_title = entry.title
        else:
            await self.async_set_unique_id(new_user_id or refresh_token[:20])
            self._abort_if_unique_id_configured()

            data: dict[str, Any] = {CONF_REFRESH_TOKEN: refresh_token}
            if new_user_id:
                data[CONF_AUTH_USER_ID] = new_user_id
            effective_auth_url = self._pending_auth_url or self._detected_url
            if effective_auth_url:
                data[CONF_AUTH_URL] = effective_auth_url
            if self._pending_api_key:
                data[CONF_API_KEY] = self._pending_api_key

            self._pending_data = data
            self._pending_title = info["title"]

        _LOGGER.debug(
            "Native Microsoft Family mobile OAuth complete; redirecting the "
            "same browser tab directly into the Family web-session phase"
        )
        return await self.async_step_start_web_auth()

    async def async_step_finish_mobile_proxy(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Exchange captured OAuth code and continue with web-session capture."""
        if self._native_mobile_abort_reason:
            reason = self._native_mobile_abort_reason
            self._native_mobile_abort_reason = None
            return self.async_abort(reason=reason)

        proxy = self._mobile_proxy
        if proxy is None or not proxy.complete or not proxy.oauth_redirect_url:
            return self.async_abort(reason="native_oauth_failed")

        redirect_url = proxy.oauth_redirect_url
        self._mobile_sso_cookies = proxy.export_cookies()
        try:
            from .auth.native_proxy import unregister_native_proxy

            await unregister_native_proxy(self.hass, proxy)
        except Exception as err:
            _LOGGER.debug("Could not close mobile OAuth proxy cleanly: %s", err)
        self._mobile_proxy = None

        try:
            info = await validate_redirect_url(self.hass, redirect_url)
        except InvalidAuth:
            return self.async_abort(reason="native_oauth_failed")

        refresh_token = info["refresh_token"]

        if self._is_existing_entry_auth_flow():
            entry = self._get_existing_entry()
            old_user_id = str(entry.data.get(CONF_AUTH_USER_ID, ""))
            new_user_id = str(info.get("user_id") or "")
            if old_user_id and new_user_id and old_user_id != new_user_id:
                return self.async_abort(reason="wrong_account")
            new_data: dict[str, Any] = {CONF_REFRESH_TOKEN: refresh_token}
            if new_user_id:
                new_data[CONF_AUTH_USER_ID] = new_user_id
            if CONF_AUTH_URL in entry.data:
                new_data[CONF_AUTH_URL] = entry.data[CONF_AUTH_URL]
            if CONF_API_KEY in entry.data:
                new_data[CONF_API_KEY] = entry.data[CONF_API_KEY]
            self._pending_data = new_data
            merged_options = dict(entry.options)
            if self._pending_options:
                merged_options.update(self._pending_options)
            self._pending_options = merged_options
            self._allow_insecure_http_auth = bool(
                merged_options.get(CONF_ALLOW_INSECURE_HTTP_AUTH, False)
            )
            self._pending_title = entry.title

            # Native/mobile-only entries always renew the web session too.
            # Legacy add-on entries keep their independent add-on cookie source.
            legacy_addon = bool(entry.data.get(CONF_AUTH_URL)) and not entry.data.get(CONF_WEB_COOKIES)
            if legacy_addon:
                return self.async_update_and_abort(
                    entry,
                    data_updates=new_data,
                    options=self._pending_options or entry.options,
                )
            return await self.async_step_start_web_auth()

        user_id = str(info.get("user_id") or "")
        await self.async_set_unique_id(user_id or refresh_token[:20])
        self._abort_if_unique_id_configured()

        data: dict[str, Any] = {CONF_REFRESH_TOKEN: refresh_token}
        if user_id:
            data[CONF_AUTH_USER_ID] = user_id
        effective_auth_url = self._pending_auth_url or self._detected_url
        if effective_auth_url:
            data[CONF_AUTH_URL] = effective_auth_url
        if self._pending_api_key:
            data[CONF_API_KEY] = self._pending_api_key

        self._pending_data = data
        self._pending_title = info["title"]

        # If an existing legacy add-on was deliberately configured, keep using it.
        if self._detected_source in ("api", "file") or self._pending_auth_url:
            return self.async_create_entry(
                title=self._pending_title,
                data=data,
                options=self._pending_options or {},
            )

        return await self.async_step_start_web_auth()

    async def async_step_start_web_auth(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Capture the account.microsoft.com Family web session."""
        if self._pending_data is None:
            return self.async_abort(reason="native_auth_state_lost")
        hass_url = URL(self._browser_hass_url())
        try:
            from .auth.native_proxy import (
                AUTH_CALLBACK_PATH,
                MicrosoftFamilyAuthProxy,
                register_native_proxy,
            )
        except Exception as err:
            _LOGGER.exception("Native Family Safety auth module could not be loaded")
            return self.async_abort(
                reason="native_auth_unavailable",
                description_placeholders={"error": str(err)},
            )

        callback_url = str(
            hass_url.with_path(AUTH_CALLBACK_PATH).with_query({"flow_id": self.flow_id})
        )
        self._native_proxy = MicrosoftFamilyAuthProxy(
            self.hass,
            callback_url=callback_url,
            start_url="https://account.microsoft.com/",
            completion_mode="web",
            initial_cookies=self._mobile_sso_cookies,
        )
        register_native_proxy(self.hass, self._native_proxy)
        proxy_url = str(hass_url.with_path(self._native_proxy.access_path))
        _LOGGER.debug(
            "Native auth flow %s starting Family web-session proxy on the same "
            "external step: initial_cookie_count=%d",
            self.flow_id,
            len(self._mobile_sso_cookies),
        )
        # Keep the same external step id as the mobile OAuth phase.  The native
        # callback redirects the already-open browser tab to proxy_url itself.
        # This intentionally avoids emitting a frontend progress event here: HA's
        # GET handler advances flows by calling async_configure(), so a frontend
        # refresh during an unfinished web phase must not be able to consume a
        # premature completion step.
        return self.async_external_step(step_id="check_mobile_proxy", url=proxy_url)

    async def async_step_wait_family_proxy(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Show Home Assistant's native progress UI while Family SSO finishes."""
        proxy = self._native_proxy
        if proxy is None or self._pending_data is None:
            _LOGGER.warning(
                "Native auth flow %s lost web proxy while waiting for Family SSO",
                self.flow_id,
            )
            return self.async_abort(reason="native_auth_state_lost")

        if proxy.complete:
            _LOGGER.debug(
                "Native auth flow %s Family SSO completed while HA progress step "
                "was active; returning progress_done -> finish_proxy",
                self.flow_id,
            )
            return self.async_show_progress_done(next_step_id="finish_proxy")

        if self._family_wait_task is None or self._family_wait_task.done():
            self._family_wait_task = self.hass.async_create_task(
                proxy.async_wait_until_complete(),
                "Microsoft Family Safety Family SSO wait",
            )
            _LOGGER.debug(
                "Native auth flow %s started HA native Family SSO progress task",
                self.flow_id,
            )

        return self.async_show_progress(
            step_id="wait_family_proxy",
            progress_action="family_sso",
            progress_task=self._family_wait_task,
        )

    async def async_step_check_proxy(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Compatibility handler for a web external step from older in-flight flows."""
        proxy = self._native_proxy
        _LOGGER.debug(
            "Native auth flow %s entered compatibility check_proxy step: "
            "proxy_present=%s complete=%s pending_data=%s",
            self.flow_id,
            proxy is not None,
            bool(proxy and proxy.complete),
            self._pending_data is not None,
        )
        if proxy is None:
            return self.async_external_step_done(next_step_id="finish_proxy")
        if not proxy.complete:
            return self.async_external_step(
                step_id="check_proxy",
                url=str(URL(self._browser_hass_url()).with_path(proxy.access_path)),
            )
        return self.async_external_step_done(next_step_id="finish_proxy")

    async def async_step_finish_proxy(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        self._family_wait_task = None
        proxy = self._native_proxy
        _LOGGER.debug(
            "Native auth flow %s entering finish_proxy: proxy_present=%s "
            "proxy_complete=%s pending_data=%s",
            self.flow_id,
            proxy is not None,
            bool(proxy and proxy.complete),
            self._pending_data is not None,
        )
        if proxy is None or not proxy.complete or self._pending_data is None:
            _LOGGER.warning(
                "Native auth flow %s cannot finish web authentication: "
                "proxy_present=%s proxy_complete=%s pending_data=%s",
                self.flow_id,
                proxy is not None,
                bool(proxy and proxy.complete),
                self._pending_data is not None,
            )
            return self.async_abort(reason="native_auth_failed")
        cookies = proxy.export_cookies()
        _LOGGER.debug(
            "Native auth flow %s exporting completed web session: cookie_count=%d",
            self.flow_id,
            len(cookies),
        )
        try:
            from .auth.native_proxy import unregister_native_proxy

            await unregister_native_proxy(self.hass, proxy)
        except Exception as err:
            _LOGGER.debug("Could not unregister native web auth proxy: %s", err)
        self._native_proxy = None
        if not cookies:
            _LOGGER.warning(
                "Native auth flow %s web proxy completed without exportable Microsoft cookies",
                self.flow_id,
            )
            return self.async_abort(reason="native_auth_failed")

        data = dict(self._pending_data)
        data[CONF_WEB_COOKIES] = cookies
        family_token = proxy.family_request_verification_token
        family_referer = proxy.family_referer
        if family_token:
            data[CONF_WEB_FAMILY_TOKEN] = family_token
        else:
            data.pop(CONF_WEB_FAMILY_TOKEN, None)
        if family_referer:
            data[CONF_WEB_FAMILY_REFERER] = family_referer
        else:
            data.pop(CONF_WEB_FAMILY_REFERER, None)
        _LOGGER.debug(
            "Native auth flow %s captured Family browser context: "
            "token_present=%s token_length=%d referer_path=%s",
            self.flow_id, bool(family_token), len(family_token or ""),
            URL(family_referer).path if family_referer else None,
        )

        if self._is_existing_entry_auth_flow():
            # Do not terminate the reauth directly from the backend-driven
            # EXTERNAL_STEP_DONE fetch. Home Assistant's frontend can issue a
            # follow-up fetch for the same flow while the terminal abort is being
            # processed, which surfaces as "Invalid flow specified" even though
            # the credentials were already updated. Keep the flow alive on a
            # final confirmation form; the user's submit request then receives
            # the terminal abort directly and no stale fetch is possible.
            self._pending_data = data
            _LOGGER.debug(
                "Native Microsoft Family credentials captured for existing entry; "
                "showing final confirmation step"
            )
            return self.async_show_form(
                step_id="reauth_success",
                data_schema=vol.Schema({}),
            )

        # Keep a successful initial setup alive on a final confirmation form.
        # Besides giving the user an explicit Continue action, this also avoids
        # removing the flow while a delayed frontend refresh may still be in flight.
        self._pending_data = data
        _LOGGER.debug(
            "Native Microsoft Family credentials captured for new entry; "
            "showing final confirmation step"
        )
        return self.async_show_form(
            step_id="auth_success",
            data_schema=vol.Schema({}),
            last_step=True,
        )

    async def async_step_auth_success(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Finish a successful initial native authentication."""
        if user_input is None:
            return self.async_show_form(
                step_id="auth_success",
                data_schema=vol.Schema({}),
                last_step=True,
            )
        if self._pending_data is None:
            return self.async_abort(reason="native_auth_state_lost")
        return self.async_create_entry(
            title=self._pending_title,
            data=dict(self._pending_data),
            options=self._pending_options or {},
        )

    async def async_step_reauth_success(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Finalize a successful reauthentication from a frontend POST."""
        if user_input is None:
            return self.async_show_form(
                step_id="reauth_success",
                data_schema=vol.Schema({}),
            )

        if self._pending_data is None:
            return self.async_abort(reason="native_auth_state_lost")

        entry = self._get_existing_entry()
        # Home Assistant's data_updates are merged into the existing entry data.
        # A reconfigure that fails to capture a new Family antiforgery token must
        # therefore explicitly overwrite the previous token/referer with None;
        # omitting the keys would silently resurrect stale browser context.
        updates = dict(self._pending_data)
        updates.setdefault(CONF_WEB_FAMILY_TOKEN, None)
        updates.setdefault(CONF_WEB_FAMILY_REFERER, None)
        _LOGGER.debug(
            "Finalizing existing-entry Microsoft Family credentials: "
            "family_token_present=%s family_referer_present=%s",
            bool(updates.get(CONF_WEB_FAMILY_TOKEN)),
            bool(updates.get(CONF_WEB_FAMILY_REFERER)),
        )
        if self.source == SOURCE_RECONFIGURE:
            _LOGGER.info(
                "Microsoft Family Safety reconfiguration completed; "
                "updating and reloading config entry"
            )
            return self.async_update_reload_and_abort(
                entry,
                data_updates=updates,
                options=self._pending_options or entry.options,
            )

        _LOGGER.info(
            "Microsoft Family Safety reauthentication completed; updating config entry"
        )
        return self.async_update_and_abort(
            entry,
            data_updates=updates,
            options=self._pending_options or entry.options,
        )

    async def async_step_auth(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manual OAuth fallback for HTTP-only Home Assistant installations."""
        errors: dict[str, str] = {}
        if user_input is not None:
            redirect_url = user_input.get(CONF_REDIRECT_URL, "").strip()
            if not redirect_url:
                errors["base"] = "no_redirect_url"
            else:
                try:
                    info = await validate_redirect_url(self.hass, redirect_url)
                    refresh_token = info["refresh_token"]
                    await self.async_set_unique_id(refresh_token[:20])
                    self._abort_if_unique_id_configured()
                    data: dict[str, Any] = {CONF_REFRESH_TOKEN: refresh_token}
                    if self._pending_auth_url or self._detected_url:
                        data[CONF_AUTH_URL] = self._pending_auth_url or self._detected_url
                    if self._pending_api_key:
                        data[CONF_API_KEY] = self._pending_api_key
                    return self.async_create_entry(
                        title=info["title"], data=data, options=self._pending_options or {}
                    )
                except InvalidAuth:
                    errors["base"] = ERROR_AUTH_FAILED
                except Exception:
                    _LOGGER.exception("Unexpected exception during manual authentication")
                    errors["base"] = "unknown"

        return self.async_show_form(
            step_id="auth",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_REDIRECT_URL): str,
                }
            ),
            description_placeholders={"auth_url": _build_auth_url()},
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manually renew Microsoft credentials from HA's Reconfigure action."""
        entry = self._get_reconfigure_entry()
        self._pending_options = dict(entry.options)
        self._allow_insecure_http_auth = bool(
            entry.options.get(CONF_ALLOW_INSECURE_HTTP_AUTH, False)
        )
        self._pending_title = entry.title
        return await self.async_step_reauth_full_confirm(user_input)

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> FlowResult:
        """Begin a linked Home Assistant reauthentication flow."""
        entry = self._get_existing_entry()
        self._pending_options = dict(entry.options)
        self._allow_insecure_http_auth = bool(
            entry.options.get(CONF_ALLOW_INSECURE_HTTP_AUTH, False)
        )
        self._pending_title = entry.title
        # Use a new step id so stale frontend translations from the prototype
        # cannot inject the old manual-{auth_url} text into reauth.
        return await self.async_step_reauth_full_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Compatibility alias for reauth flows started by older test builds."""
        return await self.async_step_reauth_full_confirm(user_input)

    async def async_step_reauth_full_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Confirm and start full mobile + web reauthentication."""
        entry = self._get_existing_entry()
        current_allow_http = bool(
            entry.options.get(CONF_ALLOW_INSECURE_HTTP_AUTH, False)
        )
        self._pending_title = entry.title

        if user_input is None:
            return self.async_show_form(
                step_id="reauth_full_confirm",
                data_schema=vol.Schema(
                    {
                        vol.Optional(
                            CONF_ALLOW_INSECURE_HTTP_AUTH,
                            default=current_allow_http,
                        ): cv.boolean,
                    }
                ),
                # Compatibility placeholders are deliberately supplied even though
                # the current translation does not reference them. This prevents
                # FORMATJS MISSING_VALUE failures if a browser still has one of the
                # pre-v10 translations cached.
                description_placeholders={
                    "auth_url": _build_auth_url(),
                    "addon_status": "native Home Assistant browser authentication",
                },
            )

        self._allow_insecure_http_auth = bool(
            user_input.get(CONF_ALLOW_INSECURE_HTTP_AUTH, current_allow_http)
        )
        self._pending_options = dict(entry.options)
        self._pending_options[CONF_ALLOW_INSECURE_HTTP_AUTH] = (
            self._allow_insecure_http_auth
        )

        try:
            return await self.async_step_start_mobile_auth()
        except NativeAuthLocalHttpRequired:
            return self.async_abort(reason="native_http_not_local")
        except NativeAuthHttpsRequired:
            return self.async_abort(reason="native_https_required")


class FamilySafetyOptionsFlow(config_entries.OptionsFlow):
    """Handle options for Microsoft Family Safety."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        current_interval = self._config_entry.options.get(
            CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL
        )
        current_platforms = self._config_entry.options.get(
            CONF_PLATFORMS, DEFAULT_PLATFORMS
        )
        current_auth_url = self._config_entry.options.get(
            CONF_AUTH_URL, self._config_entry.data.get(CONF_AUTH_URL, "")
        )
        current_api_key = self._config_entry.options.get(
            CONF_API_KEY, self._config_entry.data.get(CONF_API_KEY, "")
        )
        current_allow_insecure_http_auth = self._config_entry.options.get(
            CONF_ALLOW_INSECURE_HTTP_AUTH, False
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_UPDATE_INTERVAL,
                        default=current_interval,
                    ): vol.All(vol.Coerce(int), vol.Range(min=30, max=3600)),
                    vol.Optional(
                        CONF_PLATFORMS,
                        default=current_platforms,
                    ): cv.multi_select(
                        {p: p for p in AVAILABLE_PLATFORMS}
                    ),
                    vol.Optional(
                        CONF_ALLOW_INSECURE_HTTP_AUTH,
                        default=current_allow_insecure_http_auth,
                    ): cv.boolean,
                    vol.Optional(
                        CONF_AUTH_URL,
                        default=current_auth_url,
                    ): str,
                    vol.Optional(
                        CONF_API_KEY,
                        default=current_api_key,
                    ): str,
                }
            ),
        )


class InvalidAuth(HomeAssistantError):
    """Error to indicate authentication failure."""


class NativeAuthHttpsRequired(HomeAssistantError):
    """Native password proxying requires HTTPS unless local HTTP was opted into."""


class NativeAuthLocalHttpRequired(HomeAssistantError):
    """The opted-in HTTP proxy URL was not clearly local/private."""
