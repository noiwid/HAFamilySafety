"""Native browser-assisted Microsoft Family Safety authentication.

The user's regular browser renders Microsoft's login pages while Home Assistant
acts as a short-lived reverse proxy.  Microsoft session cookies stay in the
server-side httpx cookie jar and are exported only after the Family dashboard
has been reached.  Passwords and MFA values are never persisted or logged.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import secrets
from http.cookies import SimpleCookie
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, unquote, urljoin, urlsplit, urlunsplit

import httpx
from aiohttp import web
from yarl import URL

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.helpers.httpx_client import create_async_httpx_client

from ..const import DOMAIN

_LOGGER = logging.getLogger(__name__)

AUTH_PROXY_PATH = "/auth/microsoft_family_safety/proxy"
AUTH_CALLBACK_PATH = "/auth/microsoft_family_safety/callback"
_PROXY_REGISTRY = f"{DOMAIN}_native_auth_proxies"
_VIEWS_REGISTERED = f"{DOMAIN}_native_auth_views_registered"
_FILTER_BYPASS_INSTALLED = f"{DOMAIN}_native_auth_filter_bypass"
_HOST_MARKER = "__ms_host__"

# Authentication hosts are discovered dynamically, but only inside these
# Microsoft-owned namespace roots.  This avoids a brittle per-host allow-list
# while preventing the auth endpoint from becoming a general-purpose proxy.
_TRUSTED_MICROSOFT_DOMAIN_ROOTS = (
    "live.com",
    "microsoft.com",
    "microsoftonline.com",
)

_AUTH_COOKIE_NAMES = {
    "MSPAuth",
    "MSPProf",
    "WLSSC",
    "RPSAuth",
    "RPSSecAuth",
}

_HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
    "cookie",
    "content-length",
    "accept-encoding",
}

_RESPONSE_HEADERS_TO_DROP = _HOP_BY_HOP_HEADERS | {
    "set-cookie",
    "content-encoding",
    "content-security-policy",
    "content-security-policy-report-only",
    "x-frame-options",
    "location",
}

_REWRITABLE_ATTRS = (
    "href",
    "src",
    "action",
    "formaction",
    "data-url",
)


def _is_allowed_host(host: str | None) -> bool:
    """Return whether host is inside a trusted Microsoft auth namespace."""
    if not host:
        return False
    normalized = host.rstrip(".").lower()
    return any(
        normalized == root or normalized.endswith(f".{root}")
        for root in _TRUSTED_MICROSOFT_DOMAIN_ROOTS
    )



def _extract_family_request_verification_token(page: str) -> str | None:
    """Extract the Family SPA antiforgery token without logging its value."""
    # The supplied browser HAR shows that successful /family/api requests use
    # the hidden __RequestVerificationToken from the authenticated Family SPA.
    # Do not accept generic apiCanary/canary values here: they can look valid
    # but belong to a different antiforgery context and result in HTTP 401.
    patterns = (
        r"<input[^>]+name=[\"']__RequestVerificationToken[\"'][^>]+value=[\"']([^\"']+)",
        r"<input[^>]+value=[\"']([^\"']+)[\"'][^>]+name=[\"']__RequestVerificationToken[\"']",
        r'"__RequestVerificationToken"\s*:\s*"([^"]+)"',
    )
    for pattern in patterns:
        match = re.search(pattern, page, re.IGNORECASE)
        if match:
            return html.unescape(match.group(1))
    return None


def _cookie_dict(cookie: Any) -> dict[str, Any]:
    """Serialize a CookieJar cookie into the Playwright-compatible shape."""
    result: dict[str, Any] = {
        "name": cookie.name,
        "value": cookie.value,
        "domain": cookie.domain,
        "path": cookie.path or "/",
        "secure": bool(cookie.secure),
        "httpOnly": bool(cookie.has_nonstandard_attr("HttpOnly")),
    }
    if cookie.expires is not None:
        result["expires"] = cookie.expires
    if cookie.get_nonstandard_attr("SameSite"):
        result["sameSite"] = cookie.get_nonstandard_attr("SameSite")
    return result


class MicrosoftFamilyAuthProxy:
    """Short-lived, single-flow reverse proxy for Microsoft's consumer login."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        callback_url: str,
        token: str | None = None,
        start_url: str = "https://account.microsoft.com/",
        completion_mode: str = "web",
        initial_cookies: list[dict[str, Any]] | None = None,
    ) -> None:
        self.hass = hass
        self.token = token or secrets.token_urlsafe(24)
        self.callback_url = callback_url
        self.proxy_path = f"{AUTH_PROXY_PATH}/{self.token}"
        self._start_url = URL(start_url)
        self._last_url = self._start_url
        self._completion_mode = completion_mode
        self._oauth_redirect_url: str | None = None
        self._complete = False
        self._completion_event = asyncio.Event()
        self._family_wait_started = False
        self._family_wait_flow_notified = False
        self._flow_id = URL(callback_url).query.get("flow_id")
        self._web_recovery_attempts = 0
        self._family_bootstrap_attempts = 0
        self._family_request_verification_token: str | None = None
        self._family_referer: str | None = None
        self._closed = False
        self._expiry_task: asyncio.Task | None = None
        self._discovered_hosts: set[str] = set()
        if self._start_url.host and _is_allowed_host(self._start_url.host):
            self._discovered_hosts.add(self._start_url.host.lower())
        # Create a dedicated HA-managed httpx client. Home Assistant provides
        # a pre-built SSL context here, avoiding certifi/SSL filesystem work in
        # the event loop while keeping a separate cookie jar for this auth flow.
        # auto_cleanup=False because this short-lived proxy closes the client
        # explicitly when the flow finishes/expires.
        self._client = create_async_httpx_client(
            hass,
            auto_cleanup=False,
            follow_redirects=False,
            timeout=httpx.Timeout(connect=30.0, read=120.0, write=30.0, pool=30.0),
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                )
            },
        )
        for cookie in initial_cookies or []:
            name = cookie.get("name")
            value = cookie.get("value")
            domain = cookie.get("domain")
            if not name or value is None or not domain:
                continue
            try:
                self._client.cookies.set(
                    name,
                    value,
                    domain=domain,
                    path=cookie.get("path") or "/",
                )
            except Exception as err:
                _LOGGER.debug("Could not preload Microsoft auth cookie %s: %s", name, err)

        _LOGGER.debug(
            "Created native Microsoft Family auth proxy: mode=%s start=%s%s "
            "initial_cookie_count=%d",
            self._completion_mode,
            self._start_url.host,
            self._start_url.path,
            len(initial_cookies or []),
        )

    @property
    def complete(self) -> bool:
        return self._complete

    @property
    def family_wait_started(self) -> bool:
        """Return whether visible sign-in is done and Family SSO is running."""
        return self._family_wait_started

    async def async_wait_until_complete(self) -> None:
        """Wait until this proxy has completed its browser authentication work."""
        if self._complete:
            return
        await self._completion_event.wait()

    def _mark_complete(self) -> None:
        """Mark the browser authentication phase complete and wake waiters."""
        self._complete = True
        self._completion_event.set()

    def _start_family_wait_ui(self) -> None:
        """Move the HA external step to its native progress UI once per flow."""
        if self._completion_mode != "web":
            return
        self._family_wait_started = True
        if self._family_wait_flow_notified or not self._flow_id:
            return
        self._family_wait_flow_notified = True
        flow_id = self._flow_id

        async def _advance_flow_to_progress() -> None:
            from homeassistant.data_entry_flow import UnknownFlow

            try:
                current = self.hass.config_entries.flow.async_get(flow_id)
            except UnknownFlow:
                return
            if current.get("step_id") != "check_mobile_proxy":
                _LOGGER.debug(
                    "Microsoft Family HA progress handoff skipped: flow_id=%s "
                    "current_step=%s",
                    flow_id, current.get("step_id"),
                )
                return
            try:
                result = await self.hass.config_entries.flow.async_configure(
                    flow_id=flow_id,
                    user_input={"family_wait_started": True},
                )
            except UnknownFlow:
                return
            except Exception:
                _LOGGER.exception(
                    "Could not move Microsoft Family auth flow %s to native "
                    "Home Assistant progress step",
                    flow_id,
                )
                return
            result_type = getattr(result.get("type"), "value", result.get("type"))
            _LOGGER.debug(
                "Microsoft Family auth flow moved toward HA progress UI: "
                "flow_id=%s result_type=%s step_id=%s",
                flow_id, result_type, result.get("step_id"),
            )

        self.hass.async_create_task(
            _advance_flow_to_progress(),
            "Microsoft Family Safety HA progress handoff",
        )

    @property
    def access_path(self) -> str:
        return self.proxy_path

    @property
    def oauth_redirect_url(self) -> str | None:
        """Return the intercepted Microsoft desktop OAuth redirect URL."""
        return self._oauth_redirect_url

    @property
    def family_request_verification_token(self) -> str | None:
        """Return the Family SPA antiforgery token captured in the browser."""
        return self._family_request_verification_token

    @property
    def family_referer(self) -> str | None:
        """Return the Family SPA page that supplied the antiforgery token."""
        return self._family_referer

    def export_cookies(self) -> list[dict[str, Any]]:
        """Return only Microsoft/Live cookies required by the web session."""
        cookies: list[dict[str, Any]] = []
        for cookie in self._client.cookies.jar:
            domain = (cookie.domain or "").lstrip(".").lower()
            if domain == "microsoft.com" or domain.endswith(".microsoft.com"):
                cookies.append(_cookie_dict(cookie))
            elif domain == "live.com" or domain.endswith(".live.com"):
                cookies.append(_cookie_dict(cookie))
        return cookies

    async def async_close(self) -> None:
        if self._closed:
            return
        self._closed = True
        task = self._expiry_task
        self._expiry_task = None
        if task and task is not asyncio.current_task():
            task.cancel()
        # create_async_httpx_client wraps instance.aclose() to warn integrations
        # that close shared HA clients. This client is intentionally dedicated
        # (auto_cleanup=False), so call the httpx class implementation directly.
        await httpx.AsyncClient.aclose(self._client)

    def _has_authenticated_cookie_set(self) -> bool:
        found = {cookie.name for cookie in self._client.cookies.jar}
        return len(found & _AUTH_COOKIE_NAMES) >= 2

    def diagnostic_summary(self) -> dict[str, Any]:
        """Return a secret-free diagnostic snapshot for auth-flow logging."""
        exported = self.export_cookies()
        auth_cookie_count = sum(
            1 for cookie in self._client.cookies.jar if cookie.name in _AUTH_COOKIE_NAMES
        )
        return {
            "mode": self._completion_mode,
            "complete": self._complete,
            "exported_cookie_count": len(exported),
            "auth_cookie_count": auth_cookie_count,
            "web_recovery_attempts": self._web_recovery_attempts,
            "family_bootstrap_attempts": self._family_bootstrap_attempts,
            "family_token_present": bool(self._family_request_verification_token),
            "family_token_length": len(self._family_request_verification_token or ""),
            "family_referer_path": (URL(self._family_referer).path if self._family_referer else None),
            "last_host": self._last_url.host if self._last_url else None,
            "last_path": self._last_url.path if self._last_url else None,
        }

    def _remember_host(self, host: str | None, source: str) -> bool:
        """Remember a dynamically discovered trusted Microsoft auth host."""
        if not _is_allowed_host(host):
            return False
        normalized = str(host).rstrip(".").lower()
        if normalized not in self._discovered_hosts:
            self._discovered_hosts.add(normalized)
            _LOGGER.debug(
                "Discovered trusted Microsoft auth host %s via %s",
                normalized,
                source,
            )
        return True

    def _target_from_request(self, request: web.Request) -> URL:
        """Resolve a browser proxy request back to its Microsoft origin."""
        tail = request.match_info.get("tail", "") or ""
        tail = tail.lstrip("/")

        if not tail:
            target = self._start_url
        else:
            marker = f"{_HOST_MARKER}/"
            if tail.startswith(marker):
                encoded = tail[len(marker) :]
                host, slash, path = encoded.partition("/")
                host = host.rstrip(".").lower()
                if not self._remember_host(host, "proxy route"):
                    raise web.HTTPForbidden(
                        text="Microsoft authentication host not allowed"
                    )
                target = URL.build(
                    scheme="https",
                    host=host,
                    path=f"/{path}" if slash else "/",
                )
            else:
                # Defensive fallback for relative requests that escaped browser
                # rewriting. Resolve them against the actual last Microsoft URL.
                upstream_base = str(self._last_url)
                resolved = urlsplit(urljoin(upstream_base, tail))
                host = (resolved.hostname or "").lower()
                if not self._remember_host(host, "relative proxy request"):
                    raise web.HTTPForbidden(
                        text="Microsoft authentication host not allowed"
                    )
                target = URL(
                    urlunsplit(
                        (
                            "https",
                            host,
                            resolved.path or "/",
                            resolved.query,
                            "",
                        )
                    )
                )

        # The security-filter bypass middleware may have hidden the real query
        # string from Home Assistant's middleware chain; prefer the stashed
        # original when present so proxied OAuth parameters survive intact.
        stashed_query = request.get(_ORIGINAL_QUERY_KEY)
        if stashed_query:
            query_items = list(parse_qsl(stashed_query, keep_blank_values=True))
        else:
            query_items = list(request.query.items())

        if query_items:
            proxy_origin = f"{request.scheme}://{request.host}"
            restored_query = [
                (key, self._deproxy_value(value, proxy_origin))
                for key, value in query_items
            ]
            target = target.with_query(restored_query)
        return target

    def _proxy_bases(self, proxy_origin: str) -> tuple[str, str]:
        """Return absolute and path-only proxy bases for this flow."""
        return (
            f"{proxy_origin.rstrip('/')}{self.proxy_path}",
            self.proxy_path,
        )

    def _direct_deproxy_url(self, value: str, proxy_origin: str) -> str:
        """Undo one direct HA-proxy URL mapping without decoding payload data."""
        if not value:
            return value

        for base in self._proxy_bases(proxy_origin):
            marker = f"{base}/{_HOST_MARKER}/"
            if value.startswith(marker):
                encoded = value[len(marker) :]
                host, slash, rest = encoded.partition("/")
                host = host.rstrip(".").lower()
                if not self._remember_host(host, "deproxy"):
                    return value
                return f"https://{host}/" + rest if slash else f"https://{host}/"

            # The root proxy URL represents the current upstream page. This is
            # needed when Microsoft derives postBackUrl from window.location.
            if value == base:
                return str(self._last_url)
            if value.startswith(base + "?") or value.startswith(base + "#"):
                suffix = value[len(base) :]
                current = urlsplit(str(self._last_url))
                if suffix.startswith("?"):
                    fragment = ""
                    query_and_fragment = suffix[1:]
                    if "#" in query_and_fragment:
                        query, fragment = query_and_fragment.split("#", 1)
                    else:
                        query = query_and_fragment
                    return urlunsplit(
                        (
                            current.scheme,
                            current.netloc,
                            current.path,
                            query,
                            fragment,
                        )
                    )
                return urlunsplit(
                    (
                        current.scheme,
                        current.netloc,
                        current.path,
                        current.query,
                        suffix[1:],
                    )
                )
        return value

    def _restore_url_query(
        self,
        value: str,
        proxy_origin: str,
        *,
        depth: int,
    ) -> str:
        """Recursively restore proxied URLs nested in another URL's query."""
        if depth > 6:
            return value
        try:
            parsed = urlsplit(value)
        except ValueError:
            return value
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return value
        if not parsed.query:
            return value

        pairs = parse_qsl(parsed.query, keep_blank_values=True)
        restored = [
            (key, self._deproxy_value(item, proxy_origin, depth=depth + 1))
            for key, item in pairs
        ]
        new_query = urlencode(restored, doseq=True)
        if new_query == parsed.query:
            return value
        return urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path, new_query, parsed.fragment)
        )

    def _deproxy_value(
        self,
        value: str,
        proxy_origin: str,
        *,
        depth: int = 0,
    ) -> str:
        """Dynamically restore proxy URLs, including nested/encoded URLs.

        This is intentionally field-name agnostic: postBackUrl, returnUrl,
        redirect_uri and future Microsoft fields are treated identically.
        """
        if not isinstance(value, str) or not value or depth > 6:
            return value

        direct = self._direct_deproxy_url(value, proxy_origin)
        if direct != value:
            return self._restore_url_query(
                direct, proxy_origin, depth=depth + 1
            )

        # Even an otherwise normal Microsoft URL can carry an encoded proxy URL
        # in one of its query parameters. Parse/rebuild it recursively instead
        # of maintaining a list of parameter names.
        restored_url = self._restore_url_query(
            value, proxy_origin, depth=depth + 1
        )
        if restored_url != value:
            return restored_url

        # Handle an additional URL-encoding layer. Only accept the decoded form
        # when it actually exposes our own proxy namespace, avoiding arbitrary
        # decoding of unrelated Microsoft values.
        try:
            decoded = unquote(value)
        except Exception:
            decoded = value
        if decoded != value and (
            self.proxy_path in decoded
            or f"/{_HOST_MARKER}/" in decoded
        ):
            restored = self._deproxy_value(
                decoded, proxy_origin, depth=depth + 1
            )
            if restored != decoded:
                # Preserve the remaining percent-encoding layer. Callers such
                # as parse_qsl/urlencode already handle their own outer layer.
                return quote(restored, safe="")

        return value

    def _deproxy_object(self, obj: Any, proxy_origin: str) -> Any:
        """Recursively restore proxied URL strings in JSON-like objects."""
        if isinstance(obj, str):
            return self._deproxy_value(obj, proxy_origin)
        if isinstance(obj, list):
            return [self._deproxy_object(item, proxy_origin) for item in obj]
        if isinstance(obj, dict):
            return {
                key: self._deproxy_object(value, proxy_origin)
                for key, value in obj.items()
            }
        return obj

    def _deproxy_request_body(
        self, body: bytes | None, content_type: str, proxy_origin: str
    ) -> bytes | None:
        """Restore Microsoft protocol URLs embedded in browser request bodies."""
        if not body:
            return body

        media_type = content_type.split(";", 1)[0].strip().lower()
        try:
            if media_type == "application/x-www-form-urlencoded":
                text = body.decode("utf-8")
                pairs = parse_qsl(text, keep_blank_values=True)
                restored = [
                    (key, self._deproxy_value(value, proxy_origin))
                    for key, value in pairs
                ]
                if restored != pairs:
                    _LOGGER.debug(
                        "Restored proxied Microsoft URL value(s) in form request body"
                    )
                    return urlencode(restored, doseq=True).encode("utf-8")
                return body

            if media_type == "application/json" or media_type.endswith("+json"):
                parsed = json.loads(body.decode("utf-8"))
                restored = self._deproxy_object(parsed, proxy_origin)
                if restored != parsed:
                    _LOGGER.debug(
                        "Restored proxied Microsoft URL value(s) in JSON request body"
                    )
                    return json.dumps(
                        restored, separators=(",", ":"), ensure_ascii=False
                    ).encode("utf-8")
        except (UnicodeDecodeError, ValueError, TypeError):
            # Preserve the original body if it is not safely parseable.
            return body

        return body

    def _request_headers(self, request: web.Request, target: URL) -> dict[str, str]:
        """Copy browser headers without HA cookies or hop-by-hop metadata."""
        headers: dict[str, str] = {}
        for key, value in request.headers.items():
            lower = key.lower()
            if lower in _HOP_BY_HOP_HEADERS:
                continue
            if lower.startswith("x-forwarded-"):
                continue
            if lower.startswith("sec-fetch-"):
                continue
            if lower in {"origin", "referer"}:
                continue
            headers[key] = value

        source_host = (
            self._last_url.host
            if self._last_url.host and _is_allowed_host(self._last_url.host)
            else target.host
        )
        if request.method.upper() not in {"GET", "HEAD", "OPTIONS"}:
            headers["Origin"] = f"https://{source_host}"
        if self._last_url.host and _is_allowed_host(self._last_url.host):
            ref_path = self._last_url.raw_path_qs or "/"
            headers["Referer"] = f"https://{self._last_url.host}{ref_path}"
        return headers

    def _proxyize_url(
        self,
        value: str,
        current_url: str,
        proxy_origin: str,
        *,
        allow_relative: bool = False,
        source: str = "response",
    ) -> str:
        """Virtualize one Microsoft URL as an absolute HA proxy URL."""
        if not isinstance(value, str) or not value:
            return value
        if value.startswith(("#", "javascript:", "data:", "mailto:", "tel:")):
            return value

        proxy_origin = proxy_origin.rstrip("/")
        absolute_base, relative_base = self._proxy_bases(proxy_origin)
        if value.startswith(absolute_base + "/"):
            return value
        if value.startswith(relative_base + "/"):
            return proxy_origin + value

        candidate = html.unescape(value)
        if candidate.startswith("//"):
            candidate = "https:" + candidate
        elif allow_relative and not urlsplit(candidate).scheme:
            candidate = urljoin(current_url, candidate)

        try:
            parsed = urlsplit(candidate)
        except ValueError:
            return value
        host = (parsed.hostname or "").rstrip(".").lower()
        if parsed.scheme not in {"http", "https"} or not self._remember_host(host, source):
            return value

        path = parsed.path or "/"
        proxied = f"{absolute_base}/{_HOST_MARKER}/{host}{path}"
        if parsed.query:
            proxied += f"?{parsed.query}"
        if parsed.fragment:
            proxied += f"#{parsed.fragment}"
        return proxied

    def _proxy_response_object(
        self,
        obj: Any,
        current_url: str,
        proxy_origin: str,
    ) -> Any:
        """Dynamically virtualize Microsoft URLs in browser-facing JSON."""
        if isinstance(obj, str):
            return self._proxyize_url(
                obj,
                current_url,
                proxy_origin,
                allow_relative=False,
                source="JSON response",
            )
        if isinstance(obj, list):
            return [
                self._proxy_response_object(item, current_url, proxy_origin)
                for item in obj
            ]
        if isinstance(obj, dict):
            return {
                key: self._proxy_response_object(value, current_url, proxy_origin)
                for key, value in obj.items()
            }
        return obj

    def _rewrite_json_response(
        self, body: bytes, current_url: str, proxy_origin: str
    ) -> bytes:
        """Virtualize trusted Microsoft URLs in a JSON response."""
        try:
            parsed = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, TypeError):
            return body
        rewritten = self._proxy_response_object(parsed, current_url, proxy_origin)
        if rewritten == parsed:
            return body
        host = urlsplit(current_url).hostname or "unknown"
        _LOGGER.debug(
            "Virtualized Microsoft auth URL value(s) in JSON response from %s",
            host,
        )
        return json.dumps(
            rewritten, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")

    def _rewrite_html(self, body: str, current_url: str, proxy_origin: str) -> str:
        """Virtualize Microsoft browser navigations and install runtime routing."""
        proxy_origin = proxy_origin.rstrip("/")
        current_host = urlsplit(current_url).hostname or "account.microsoft.com"

        # Attribute URLs are actual browser navigation/resource targets, so they
        # can safely be virtualized. Protocol values elsewhere are handled by the
        # generic response/request transformers instead of a field-name list.
        attr_pattern = re.compile(
            rf"(?P<prefix>\b(?:{'|'.join(_REWRITABLE_ATTRS)})\s*=\s*[\"'])(?P<url>[^\"']+)(?P<quote>[\"'])",
            re.IGNORECASE,
        )

        def attr_repl(match: re.Match[str]) -> str:
            url = self._proxyize_url(
                match.group("url"),
                current_url,
                proxy_origin,
                allow_relative=True,
                source="HTML attribute",
            )
            return f"{match.group('prefix')}{url}{match.group('quote')}"

        body = attr_pattern.sub(attr_repl, body)

        # Dynamically rewrite absolute Microsoft URL literals in inline config.
        # There is no per-host list: the host is accepted only if it belongs to a
        # trusted Microsoft namespace. Keeping the result absolute preserves
        # Microsoft code that calls new URL(value) without a base URL.
        plain_url_prefix = re.compile(
            r"(?P<prefix>https?:)?//(?P<host>[A-Za-z0-9.-]+)",
            re.IGNORECASE,
        )

        def plain_repl(match: re.Match[str]) -> str:
            host = match.group("host").rstrip(".").lower()
            if not self._remember_host(host, "inline HTML/JS"):
                return match.group(0)
            return (
                f"{proxy_origin}{self.proxy_path}/{_HOST_MARKER}/{host}"
            )

        body = plain_url_prefix.sub(plain_repl, body)

        escaped_url_prefix = re.compile(
            r"https?:\\/\\/(?P<host>[A-Za-z0-9.-]+)",
            re.IGNORECASE,
        )

        def escaped_repl(match: re.Match[str]) -> str:
            host = match.group("host").rstrip(".").lower()
            if not self._remember_host(host, "escaped inline HTML/JS"):
                return match.group(0)
            prefix = f"{proxy_origin}{self.proxy_path}/{_HOST_MARKER}/{host}"
            return prefix.replace("/", r"\/")

        body = escaped_url_prefix.sub(escaped_repl, body)

        proxy_path_js = json.dumps(self.proxy_path)
        proxy_origin_js = json.dumps(proxy_origin)
        host_js = json.dumps(current_host)
        roots_js = json.dumps(list(_TRUSTED_MICROSOFT_DOMAIN_ROOTS))
        shim = f"""<script>
(function(){{
  const po={proxy_origin_js}, pp={proxy_path_js}, ch={host_js}, roots={roots_js};
  function trusted(h){{
    h=(h||"").toLowerCase().replace(/\\.$/,"");
    return roots.some(r=>h===r || h.endsWith("."+r));
  }}
  function rw(u){{
    if(typeof u!=="string" || !u) return u;
    if(u.startsWith(po+pp+"/")) return u;
    if(u.startsWith(pp+"/")) return po+u;
    try {{
      const p=new URL(u,"https://"+ch+"/");
      if(!trusted(p.hostname)) return u;
      return po+pp+"/{_HOST_MARKER}/"+p.hostname.toLowerCase()+p.pathname+p.search+p.hash;
    }} catch(e) {{ return u; }}
  }}
  const fo=window.fetch;
  if(fo) window.fetch=function(input,init){{
    if(typeof input==="string") input=rw(input);
    else if(input && input.url) input=new Request(rw(input.url),input);
    return fo.call(this,input,init);
  }};
  const xo=XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open=function(method,url){{
    if(typeof url==="string") arguments[1]=rw(url);
    return xo.apply(this,arguments);
  }};
  if(navigator.sendBeacon){{
    const bo=navigator.sendBeacon.bind(navigator);
    navigator.sendBeacon=function(url,data){{ return bo(rw(String(url)),data); }};
  }}
  const wo=window.open;
  if(wo) window.open=function(url){{
    if(typeof url==="string") arguments[0]=rw(url);
    return wo.apply(this,arguments);
  }};
  try {{
    const la=Location.prototype.assign, lr=Location.prototype.replace;
    Location.prototype.assign=function(url){{ return la.call(this,rw(String(url))); }};
    Location.prototype.replace=function(url){{ return lr.call(this,rw(String(url))); }};
  }} catch(e) {{}}
  for(const n of ["pushState","replaceState"]){{
    try {{
      const ho=history[n].bind(history);
      history[n]=function(state,title,url){{
        return ho(state,title,url==null?url:rw(String(url)));
      }};
    }} catch(e) {{}}
  }}
  function fixOne(el){{
    if(!el || !el.getAttribute) return;
    for(const a of ["href","src","action","formaction","data-url"]){{
      if(el.hasAttribute(a)) el.setAttribute(a,rw(el.getAttribute(a)));
    }}
  }}
  function fix(root){{
    fixOne(root);
    const nodes=(root&&root.querySelectorAll)?root.querySelectorAll("[href],[src],[action],[formaction],[data-url]"):[];
    for(const el of nodes) fixOne(el);
  }}
  document.addEventListener("click",e=>{{
    const a=e.target&&e.target.closest?e.target.closest("a[href]"):null;
    if(a) a.setAttribute("href",rw(a.getAttribute("href")));
  }},true);
  document.addEventListener("submit",e=>{{
    const f=e.target;
    if(f&&f.getAttribute&&f.hasAttribute("action"))
      f.setAttribute("action",rw(f.getAttribute("action")));
  }},true);
  try {{
    const fs=HTMLFormElement.prototype.submit;
    HTMLFormElement.prototype.submit=function(){{ fixOne(this); return fs.call(this); }};
    if(HTMLFormElement.prototype.requestSubmit){{
      const fr=HTMLFormElement.prototype.requestSubmit;
      HTMLFormElement.prototype.requestSubmit=function(){{ fixOne(this); return fr.apply(this,arguments); }};
    }}
  }} catch(e) {{}}
  document.addEventListener("DOMContentLoaded",()=>{{
    fix(document);
    new MutationObserver(ms=>ms.forEach(m=>m.addedNodes.forEach(fix)))
      .observe(document.documentElement,{{subtree:true,childList:true}});
  }});
}})();
</script>"""

        head_match = re.search(r"<head\b[^>]*>", body, re.IGNORECASE)
        if head_match:
            pos = head_match.end()
            return body[:pos] + shim + body[pos:]
        return shim + body


    def _browser_cookie_headers(self, response: httpx.Response, request: web.Request) -> list[str]:
        """Mirror Microsoft cookies onto the scoped HA proxy origin.

        HTTP redirects are deliberately exposed to the browser. Mirror the
        current jar as well as explicit deletion/update Set-Cookie headers for
        every hop so Microsoft's browser JavaScript sees a consistent cookie
        view on the scoped HA proxy origin while the upstream jar remains
        authoritative.
        """
        result: list[str] = []
        secure_proxy = request.scheme == "https"
        final_host = (URL(str(response.url)).host or "").lower()
        seen: set[str] = set()

        def append_cookie(
            name: str,
            value: str,
            *,
            http_only: bool = False,
            secure: bool = False,
            same_site: str = "",
            max_age: str = "",
        ) -> None:
            # One browser origin cannot preserve Microsoft's domain partitioning.
            # For the active Microsoft host, the most recent value per name is
            # sufficient; the server-side jar remains authoritative upstream.
            key = name.lower()
            if key in seen:
                return
            seen.add(key)
            parts = [f"{name}={value}", f"Path={self.proxy_path}/"]
            if max_age:
                parts.append(f"Max-Age={max_age}")
            if http_only:
                parts.append("HttpOnly")
            if secure_proxy and secure:
                parts.append("Secure")
            same_site = (same_site or "").strip()
            if same_site and not (same_site.lower() == "none" and not secure_proxy):
                parts.append(f"SameSite={same_site}")
            result.append("; ".join(parts))

        # Explicit Set-Cookie headers first so deletes/updates win over the jar.
        try:
            raw_headers = response.headers.get_list("set-cookie")
        except Exception:
            raw = response.headers.get("set-cookie")
            raw_headers = [raw] if raw else []
        for raw in raw_headers:
            if not raw:
                continue
            parsed = SimpleCookie()
            try:
                parsed.load(raw)
            except Exception:
                continue
            for name, morsel in parsed.items():
                append_cookie(
                    name,
                    morsel.value,
                    http_only=bool(morsel["httponly"]),
                    secure=bool(morsel["secure"]),
                    same_site=morsel["samesite"] or "",
                    max_age=morsel["max-age"] or "",
                )

        # Include the current server-side cookie jar as well. Redirects are now
        # browser-visible, so each hop is mirrored independently while the
        # authoritative upstream jar preserves Microsoft's real domain scoping.
        for cookie in self._client.cookies.jar:
            domain = (cookie.domain or "").lstrip(".").lower()
            if not domain or not final_host:
                continue
            if not (final_host == domain or final_host.endswith("." + domain)):
                continue
            append_cookie(
                cookie.name,
                cookie.value,
                http_only=bool(cookie.has_nonstandard_attr("HttpOnly")),
                secure=bool(cookie.secure),
                same_site=cookie.get_nonstandard_attr("SameSite") or "",
            )
        return result

    def _response_headers(self, response: httpx.Response) -> Mapping[str, str]:
        result: dict[str, str] = {}
        for key, value in response.headers.items():
            if key.lower() in _RESPONSE_HEADERS_TO_DROP:
                continue
            result[key] = value
        result["Cache-Control"] = "no-store"
        return result

    async def handle(self, request: web.Request) -> web.StreamResponse:
        if self._closed:
            raise web.HTTPGone(text="Authentication flow is closed")

        target = self._target_from_request(request)
        headers = self._request_headers(request, target)
        body = await request.read() if request.can_read_body else None
        proxy_origin = f"{request.scheme}://{request.host}"
        body = self._deproxy_request_body(
            body,
            request.headers.get("Content-Type", ""),
            proxy_origin,
        )

        _LOGGER.debug(
            "Native Family auth proxy request: %s https://%s%s",
            request.method,
            target.host,
            target.path,
        )
        try:
            response = await self._client.request(
                request.method,
                str(target),
                headers=headers,
                content=body if body else None,
            )
        except httpx.TimeoutException as err:
            _LOGGER.warning("Microsoft authentication request timed out: %s", err)
            raise web.HTTPGatewayTimeout(text="Microsoft authentication request timed out") from err
        except httpx.HTTPError as err:
            _LOGGER.warning("Microsoft authentication request failed: %s", err)
            raise web.HTTPBadGateway(text="Microsoft authentication request failed") from err

        current = URL(str(response.url))
        if not self._remember_host(current.host, "upstream response"):
            _LOGGER.warning("Microsoft auth response came from unexpected host %s", current.host)
            raise web.HTTPBadGateway(text="Unexpected Microsoft authentication response")
        self._last_url = current
        _LOGGER.debug(
            "Native Family auth proxy response: HTTP %s https://%s%s",
            response.status_code,
            current.host,
            current.path,
        )

        # Preserve Microsoft's redirect state in the real browser. Silent SSO
        # callbacks rely on browser-visible navigation/location semantics;
        # following redirects inside Home Assistant collapses that state and can
        # leave complete-client-signin-oauth-silent as a blank top-level page.
        if response.status_code in {301, 302, 303, 307, 308}:
            location = response.headers.get("location")
            if not location:
                raise web.HTTPBadGateway(text="Microsoft redirect did not include Location")
            resolved = URL(urljoin(str(current), location))
            if not self._remember_host(resolved.host, "upstream redirect"):
                _LOGGER.warning("Microsoft auth redirected to unexpected host %s", resolved.host)
                raise web.HTTPBadGateway(text="Unexpected Microsoft authentication redirect")

            # The mobile API's registered desktop redirect is terminal for this
            # proxy. Capture the code/error before the browser navigates there.
            if (
                self._completion_mode == "oauth"
                and resolved.host == "login.live.com"
                and resolved.path == "/oauth20_desktop.srf"
                and ("code" in resolved.query or "error" in resolved.query)
            ):
                self._oauth_redirect_url = str(resolved)
                self._complete = "code" in resolved.query
                if self._complete:
                    _LOGGER.info(
                        "Native Microsoft Family mobile OAuth redirect captured: "
                        "code_present=True cookie_count=%d",
                        len(self.export_cookies()),
                    )
                else:
                    _LOGGER.warning(
                        "Microsoft Family mobile OAuth returned an error redirect: error=%s",
                        resolved.query.get("error", "unknown"),
                    )
                raise web.HTTPFound(location=self.callback_url)

            proxied_location = self._proxyize_url(
                str(resolved),
                str(current),
                proxy_origin,
                allow_relative=False,
                source="upstream redirect",
            )
            if proxied_location == str(resolved):
                raise web.HTTPBadGateway(text="Microsoft redirect could not be proxied")

            outgoing = web.Response(
                status=response.status_code,
                headers=self._response_headers(response),
            )
            outgoing.headers["Location"] = proxied_location
            mirrored = self._browser_cookie_headers(response, request)
            for cookie_header in mirrored:
                outgoing.headers.add("Set-Cookie", cookie_header)
            if mirrored:
                _LOGGER.debug(
                    "Mirrored %d Microsoft auth cookie(s) to scoped proxy origin",
                    len(mirrored),
                )
            _LOGGER.debug(
                "Forwarding Microsoft redirect to browser: HTTP %s https://%s%s",
                response.status_code,
                resolved.host,
                resolved.path,
            )
            return outgoing

        final = current

        # Some Microsoft variants return the desktop redirect as a normal 200
        # document instead of an HTTP redirect. Keep this fallback for both.
        if (
            self._completion_mode == "oauth"
            and final.host == "login.live.com"
            and final.path == "/oauth20_desktop.srf"
            and ("code" in final.query or "error" in final.query)
        ):
            self._oauth_redirect_url = str(final)
            self._complete = "code" in final.query
            if self._complete:
                _LOGGER.info(
                    "Native Microsoft Family mobile OAuth redirect captured from 200 response: "
                    "code_present=True cookie_count=%d",
                    len(self.export_cookies()),
                )
            else:
                _LOGGER.warning(
                    "Microsoft Family mobile OAuth returned an error document: error=%s",
                    final.query.get("error", "unknown"),
                )
            raise web.HTTPFound(location=self.callback_url)

        def _is_authenticated_account_destination(url: URL, status_code: int) -> bool:
            """Return whether an account.microsoft.com navigation proves SSO is live.

            Microsoft does not consistently land on /family after silent SSO.
            Current portal variants may finish on / or /account instead.  These
            top-level document destinations are sufficient once the characteristic
            Microsoft auth-cookie set is present.  Bundle/API paths are deliberately
            excluded so a background resource cannot complete the flow.
            """
            if status_code != 200 or url.host != "account.microsoft.com":
                return False
            path = url.path.rstrip("/") or "/"
            if "complete-signin-oauth" in str(url) or "complete-client-signin" in str(url):
                return False
            return path in ("/", "/account") or path.startswith("/family")

        # Microsoft currently sometimes ends the silent account SSO bootstrap on
        # /auth/client-signin-oauth-silent-error/INIT-FAIL-... even though the
        # relevant Microsoft cookies have already been established.  A manual
        # second/third press of HA's Open website button merely starts another
        # top-level account.microsoft.com navigation and is why that workaround
        # appeared to help.  Perform the same recovery in the *browser* so all
        # navigation/origin semantics remain intact.  Limit retries to avoid a
        # loop if Microsoft is genuinely unable to establish the session.
        if (
            self._completion_mode == "web"
            and response.status_code == 200
            and final.host == "account.microsoft.com"
            and final.path.startswith("/auth/client-signin-oauth-silent-error/")
            and self._family_bootstrap_attempts == 0
            and self._web_recovery_attempts < 3
        ):
            self._web_recovery_attempts += 1
            retry_location = f"{proxy_origin}{self.proxy_path}"
            _LOGGER.debug(
                "Microsoft Family silent SSO returned %s; retrying top-level "
                "account navigation in the same browser tab (%d/3)",
                final.path.rsplit("/", 1)[-1],
                self._web_recovery_attempts,
            )
            raise web.HTTPFound(location=retry_location)

        authenticated_account_page = (
            self._completion_mode == "web"
            and self._has_authenticated_cookie_set()
            and _is_authenticated_account_destination(final, response.status_code)
        )

        # The private Family API needs browser-established SPA context beyond the
        # generic /account login. Bootstrap that context in the already-open real
        # browser tab and capture the hidden request-verification token there.
        if (
            self._completion_mode == "web"
            and response.status_code == 200
            and final.host == "account.microsoft.com"
            and final.path.startswith("/family")
        ):
            self._start_family_wait_ui()
            content_type = response.headers.get("content-type", "")
            page = ""
            if "text/html" in content_type.lower():
                encoding = response.encoding or "utf-8"
                page = response.content.decode(encoding, errors="replace")
            token = _extract_family_request_verification_token(page) if page else None
            if token and self._has_authenticated_cookie_set():
                self._family_request_verification_token = token
                self._family_referer = str(final)
                self._mark_complete()
                summary = self.diagnostic_summary()
                _LOGGER.info(
                    "Native Microsoft Family browser context captured at https://%s%s: "
                    "exported_cookies=%d auth_cookies=%d family_token_present=True "
                    "family_token_length=%d bootstrap_attempts=%d",
                    final.host, final.path, summary["exported_cookie_count"],
                    summary["auth_cookie_count"], summary["family_token_length"],
                    summary["family_bootstrap_attempts"],
                )
                raise web.HTTPFound(location=self.callback_url)
            _LOGGER.debug(
                "Microsoft Family browser page reached without usable antiforgery token: "
                "path=%s status=%s bootstrap_attempts=%d",
                final.path, response.status_code, self._family_bootstrap_attempts,
            )

        def _next_family_bootstrap_target() -> str | None:
            # The supplied 2026-08-12 browser HAR reaches the authenticated Family
            # SPA through complete-silent-signin -> /family/windows/home/direct.
            # The hidden token from that page is then reused by successful Family
            # API calls. Reproduce that route first so Microsoft can establish any
            # Family-specific compact-ticket/session state before token capture.
            targets = (
                "https://account.microsoft.com/family/windows/home/direct?fref=coldstartv2&refd=account.microsoft.com",
                "https://account.microsoft.com/family/windows/coldstart?refd=account.microsoft.com",
                "https://account.microsoft.com/family/home",
            )
            if self._family_bootstrap_attempts >= len(targets):
                return None
            target = targets[self._family_bootstrap_attempts]
            self._family_bootstrap_attempts += 1
            proxied = self._proxyize_url(
                target, str(final), proxy_origin, allow_relative=False,
                source="family browser bootstrap",
            )
            return proxied if proxied != target else None

        if self._completion_mode == "web" and authenticated_account_page:
            next_target = _next_family_bootstrap_target()
            if next_target:
                self._start_family_wait_ui()
                _LOGGER.debug(
                    "Microsoft account SSO verified; bootstrapping Family browser context "
                    "in same tab (%d/3)", self._family_bootstrap_attempts,
                )
                raise web.HTTPFound(location=next_target)
            self._mark_complete()
            _LOGGER.warning(
                "Native Microsoft account authentication completed but Family browser "
                "context could not be captured after %d attempts; exporting account "
                "cookies without Family antiforgery token",
                self._family_bootstrap_attempts,
            )
            raise web.HTTPFound(location=self.callback_url)

        family_watchdog_target: str | None = None
        if (
            self._completion_mode == "web"
            and response.status_code == 200
            and final.host == "www.microsoft.com"
            and "family-safety" in final.path
        ):
            next_target = _next_family_bootstrap_target()
            if next_target:
                _LOGGER.debug(
                    "Family browser bootstrap reached Microsoft marketing page; "
                    "trying next Family landing route in same tab (%d/3)",
                    self._family_bootstrap_attempts,
                )
                raise web.HTTPFound(location=next_target)
            self._mark_complete()
            _LOGGER.warning(
                "Family browser bootstrap exhausted all landing routes on the "
                "Microsoft marketing page; completing Home Assistant authentication "
                "without a Family antiforgery token"
            )
            raise web.HTTPFound(location=self.callback_url)

        # A Family landing route can start a second Microsoft silent-SSO round.
        # In a proxied top-level tab Microsoft occasionally leaves
        # complete-client-signin-oauth-silent rendered without performing the
        # final JavaScript navigation.  Do not let that blank/stalled document
        # hold the Home Assistant flow forever.  Inject a delayed browser-side
        # watchdog: normal Microsoft JavaScript gets several seconds to navigate;
        # if the document is still alive afterwards we try the next Family route.
        # On the last attempt the watchdog returns to our callback and completes
        # authentication with the valid account cookies even if no Family token
        # could be captured.
        if (
            self._completion_mode == "web"
            and response.status_code == 200
            and final.host == "account.microsoft.com"
            and final.path == "/auth/complete-client-signin-oauth-silent"
            and self._family_bootstrap_attempts > 0
        ):
            family_watchdog_target = _next_family_bootstrap_target()
            if family_watchdog_target:
                _LOGGER.debug(
                    "Family browser silent-SSO page reached; arming %.1fs watchdog "
                    "for next Family landing route (%d/3)",
                    55.0,
                    self._family_bootstrap_attempts,
                )
            else:
                self._mark_complete()
                family_watchdog_target = self.callback_url
                _LOGGER.warning(
                    "Family browser silent-SSO page reached after all bootstrap "
                    "routes; arming %.1fs watchdog to complete Home Assistant "
                    "authentication without a Family antiforgery token",
                    55.0,
                )

        # During Family bootstrap the proxied Microsoft client-signin page can
        # report INIT-FAIL and still recover later via complete-sso-with-redirect.
        # Older proxy builds proved that this recovery can take ~45 seconds.
        # Do not immediately bounce back to /account here, because that aborts
        # Microsoft's own recovery and advances to the next bootstrap route.
        if (
            self._completion_mode == "web"
            and response.status_code == 200
            and final.host == "account.microsoft.com"
            and final.path.startswith("/auth/client-signin-oauth-silent-error/")
            and self._family_bootstrap_attempts > 0
        ):
            family_watchdog_target = _next_family_bootstrap_target()
            if family_watchdog_target:
                _LOGGER.debug(
                    "Family browser INIT-FAIL reached during bootstrap; preserving "
                    "Microsoft recovery page and arming %.1fs fallback for next "
                    "Family landing route (%d/3)",
                    55.0,
                    self._family_bootstrap_attempts,
                )
            else:
                self._mark_complete()
                family_watchdog_target = self.callback_url
                _LOGGER.warning(
                    "Family browser INIT-FAIL persisted after all bootstrap routes; "
                    "arming %.1fs fallback to complete Home Assistant authentication "
                    "without a Family antiforgery token",
                    55.0,
                )

        content_type = response.headers.get("content-type", "")
        content = response.content
        if "text/html" in content_type.lower():
            encoding = response.encoding or "utf-8"
            text = content.decode(encoding, errors="replace")
            text = self._rewrite_html(
                text,
                str(final),
                proxy_origin,
            )
            # Add user-facing status UI only to top-level browser documents.
            # Hidden Microsoft iframes also pass through this proxy, so avoid
            # injecting visible controls into those documents.
            sec_fetch_dest = request.headers.get("Sec-Fetch-Dest", "").lower()
            is_top_level_document = sec_fetch_dest in {"", "document"}
            auth_ui = ""
            if is_top_level_document:
                # Keep the auth-proxy status UI in English regardless of browser or
                # Home Assistant locale. This makes screenshots/log-assisted testing
                # consistent across installations.
                if family_watchdog_target:
                    waiting_title = "Preparing Microsoft Family Safety"
                    waiting_text = (
                        "The visible sign-in is complete. Microsoft is now preparing "
                        "the Family session in the background. Keep this window open; "
                        "Home Assistant will continue automatically."
                    )
                    waiting_stage = "Waiting for Microsoft Family SSO"
                    waiting_remaining = "Up to"
                    waiting_seconds = "seconds remaining"

                    # This overlay is deliberately non-interactive so it cannot block
                    # Microsoft's own page or JavaScript. The browser leaves the page
                    # immediately if Microsoft finishes before the watchdog expires.
                    auth_ui = (
                        '<style id="ha-family-auth-style">'
                        '@keyframes haFamSpin{to{transform:rotate(360deg)}}'
                        '@keyframes haFamPulse{0%,100%{opacity:.35}50%{opacity:1}}'
                        '@keyframes haFamProgress{from{width:0}to{width:100%}}'
                        '#ha-family-auth-wait{position:fixed;z-index:2147483647;'
                        'left:50%;top:50%;width:min(680px,calc(100vw - 32px));max-width:680px;'
                        'transform:translate(-50%,-50%);margin:0;'
                        'box-sizing:border-box;padding:18px 20px;border-radius:14px;'
                        'background:rgba(255,255,255,.98);color:#202124;'
                        'box-shadow:0 8px 28px rgba(0,0,0,.30);font:14px/1.45 sans-serif;'
                        'pointer-events:none}'
                        '#ha-family-auth-head{display:flex;align-items:center;gap:12px}'
                        '#ha-family-auth-spinner{width:24px;height:24px;border:3px solid #dadce0;'
                        'border-top-color:#1a73e8;border-radius:50%;flex:0 0 auto;'
                        'animation:haFamSpin .9s linear infinite}'
                        '#ha-family-auth-title{font-size:16px;font-weight:600}'
                        '#ha-family-auth-stage{margin-top:10px;font-weight:600}'
                        '#ha-family-auth-dots span{animation:haFamPulse 1.2s ease-in-out infinite}'
                        '#ha-family-auth-dots span:nth-child(2){animation-delay:.2s}'
                        '#ha-family-auth-dots span:nth-child(3){animation-delay:.4s}'
                        '#ha-family-auth-progress{height:5px;margin-top:12px;border-radius:4px;'
                        'overflow:hidden;background:#e8eaed}'
                        '#ha-family-auth-progress>span{display:block;height:100%;width:0;'
                        'background:#1a73e8;animation:haFamProgress 55s linear forwards}'
                        '#ha-family-auth-time{margin-top:7px;font-size:12px;color:#5f6368}'
                        '</style>'
                        '<div id="ha-family-auth-wait" role="status" aria-live="polite">'
                        '<div id="ha-family-auth-head"><span id="ha-family-auth-spinner"></span>'
                        '<div><div id="ha-family-auth-title">'
                        + html.escape(waiting_title)
                        + '</div><div>'
                        + html.escape(waiting_text)
                        + '</div></div></div>'
                        '<div id="ha-family-auth-stage">'
                        + html.escape(waiting_stage)
                        + '<span id="ha-family-auth-dots"><span>.</span><span>.</span><span>.</span></span>'
                        '</div><div id="ha-family-auth-progress"><span></span></div>'
                        '<div id="ha-family-auth-time">'
                        + html.escape(waiting_remaining)
                        + ' <span id="ha-family-auth-seconds">55</span> '
                        + html.escape(waiting_seconds)
                        + '</div></div>'
                        '<script>(function(){'
                        'const u=' + json.dumps(family_watchdog_target) + ';'
                        'const started=Date.now();const maxMs=55000;'
                        'const el=document.getElementById("ha-family-auth-seconds");'
                        'const tick=function(){if(!el)return;const left=Math.max(0,'
                        'Math.ceil((maxMs-(Date.now()-started))/1000));el.textContent=String(left);};'
                        'tick();const timer=setInterval(tick,250);'
                        'setTimeout(function(){clearInterval(timer);try{window.location.replace(u);}'
                        'catch(e){window.location.href=u;}},maxMs);})();</script>'
                    )
                elif self._completion_mode in {"oauth", "web"} and final.host in {
                    "login.live.com",
                    "login.microsoft.com",
                    "login.microsoftonline.com",
                    "account.microsoft.com",
                }:
                    notice_title = "Home Assistant · Microsoft Family Safety"
                    notice_text = (
                        "This window is part of the Home Assistant setup. After the "
                        "Microsoft sign-in, the remaining steps continue automatically. "
                        "Please keep this window open."
                    )
                    auth_ui = (
                        '<style id="ha-family-auth-notice-style">'
                        '@keyframes haFamNoticeSpin{to{transform:rotate(360deg)}}'
                        '#ha-family-auth-notice{position:fixed;z-index:2147483647;'
                        'left:50%;top:22px;transform:translateX(-50%);'
                        'width:min(560px,calc(100vw - 32px));box-sizing:border-box;'
                        'padding:12px 16px;border-radius:12px;background:rgba(255,255,255,.97);'
                        'color:#202124;box-shadow:0 4px 18px rgba(0,0,0,.22);'
                        'font:13px/1.45 sans-serif;pointer-events:none}'
                        '#ha-family-auth-notice-row{display:flex;align-items:center;gap:11px}'
                        '#ha-family-auth-notice-spinner{width:20px;height:20px;flex:0 0 auto;'
                        'border:3px solid #dadce0;border-top-color:#1a73e8;border-radius:50%;'
                        'animation:haFamNoticeSpin .9s linear infinite}'
                        '#ha-family-auth-notice b{display:block;font-size:14px;margin-bottom:2px}'
                        '</style><div id="ha-family-auth-notice" role="status" aria-live="polite">'
                        '<div id="ha-family-auth-notice-row">'
                        '<span id="ha-family-auth-notice-spinner"></span><div><b>'
                        + html.escape(notice_title)
                        + '</b><span>'
                        + html.escape(notice_text)
                        + '</span></div></div></div>'
                    )

            if auth_ui:
                if re.search(r"</body\s*>", text, re.IGNORECASE):
                    text = re.sub(
                        r"</body\s*>", auth_ui + "</body>", text,
                        count=1, flags=re.IGNORECASE,
                    )
                else:
                    text += auth_ui
            content = text.encode("utf-8")
            content_type = re.sub(
                r"charset=[^;]+", "charset=utf-8", content_type, flags=re.IGNORECASE
            )
        elif (
            "application/json" in content_type.lower()
            or "+json" in content_type.lower()
        ):
            content = self._rewrite_json_response(
                content,
                str(final),
                proxy_origin,
            )

        outgoing = web.Response(
            status=response.status_code,
            body=content,
            headers=self._response_headers(response),
            content_type=None,
        )
        mirrored = self._browser_cookie_headers(response, request)
        for cookie_header in mirrored:
            outgoing.headers.add("Set-Cookie", cookie_header)
        if mirrored:
            _LOGGER.debug(
                "Mirrored %d Microsoft auth cookie(s) to scoped proxy origin",
                len(mirrored),
            )
        return outgoing


class MicrosoftFamilyAuthorizationProxyView(HomeAssistantView):
    """Dispatch auth proxy requests by an unguessable per-flow token."""

    url = f"{AUTH_PROXY_PATH}/{{token}}"
    extra_urls = [f"{AUTH_PROXY_PATH}/{{token}}/{{tail:.*}}"]
    name = "api:microsoft_family_safety:auth_proxy"
    requires_auth = False

    async def _handle(
        self,
        request: web.Request,
        token: str | None = None,
        tail: str | None = None,
    ) -> web.StreamResponse:
        """Dispatch a proxied auth request.

        HomeAssistantView passes route variables as keyword arguments. The
        optional ``tail`` argument is intentionally accepted for ``extra_urls``
        even though target resolution reads it from ``request.match_info``.
        """
        hass: HomeAssistant = request.app["hass"]
        token = token or request.match_info.get("token")
        if not token:
            raise web.HTTPBadRequest(text="Missing authentication flow token")
        proxy = hass.data.get(_PROXY_REGISTRY, {}).get(token)
        if not isinstance(proxy, MicrosoftFamilyAuthProxy):
            raise web.HTTPNotFound(text="Authentication flow not found")
        return await proxy.handle(request)

    get = _handle
    post = _handle
    put = _handle
    patch = _handle
    delete = _handle
    options = _handle
    head = _handle


class MicrosoftFamilyAuthorizationCallbackView(HomeAssistantView):
    """Resume the config flow once Microsoft authentication is complete."""

    url = AUTH_CALLBACK_PATH
    name = "api:microsoft_family_safety:auth_callback"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        flow_id = request.query.get("flow_id")
        if not flow_id:
            raise web.HTTPBadRequest(text="Missing flow_id")
        try:
            from homeassistant.data_entry_flow import UnknownFlow

            try:
                before = hass.config_entries.flow.async_get(flow_id)
                before_step = before.get("step_id")
                before_source = (before.get("context") or {}).get("source")
            except UnknownFlow:
                before_step = None
                before_source = None

            matching_proxy_summaries = []
            for candidate in hass.data.get(_PROXY_REGISTRY, {}).values():
                if not isinstance(candidate, MicrosoftFamilyAuthProxy):
                    continue
                if f"flow_id={flow_id}" not in candidate.callback_url:
                    continue
                matching_proxy_summaries.append(candidate.diagnostic_summary())

            _LOGGER.debug(
                "Native auth callback entering HA flow: flow_id=%s current_step=%s "
                "source=%s proxies=%s",
                flow_id,
                before_step,
                before_source,
                matching_proxy_summaries,
            )

            # After the external-step -> native-progress handoff, the progress task
            # owns Home Assistant advancement.  The browser's final callback is only
            # a completion page at that point; configuring a progress/form step here
            # would race the frontend and could accidentally submit the final form.
            if before_step in {
                "wait_family_proxy",
                "finish_proxy",
                "reauth_success",
                "auth_success",
            }:
                from homeassistant.data_entry_flow import EVENT_DATA_ENTRY_FLOW_PROGRESSED

                _LOGGER.debug(
                    "Native Microsoft Family browser completion arrived after HA "
                    "progress handoff; leaving flow advancement to Home Assistant: "
                    "flow_id=%s current_step=%s",
                    flow_id, before_step,
                )
                # Re-notify the frontend in case the original progress event was
                # missed while focus was in the Microsoft window.
                hass.bus.async_fire_internal(
                    EVENT_DATA_ENTRY_FLOW_PROGRESSED,
                    {
                        "handler": DOMAIN,
                        "flow_id": flow_id,
                        "refresh": True,
                    },
                )
                result = {
                    "type": "browser_complete",
                    "step_id": before_step,
                }
            else:
                # Mobile OAuth and any web completion that occurs before the HA
                # progress handoff still advance through the canonical external step.
                result = await hass.config_entries.flow.async_configure(
                    flow_id=flow_id, user_input={"native_callback": True}
                )
        except UnknownFlow as err:
            in_progress = [
                {
                    "flow_id": item.get("flow_id"),
                    "step_id": item.get("step_id"),
                    "source": (item.get("context") or {}).get("source"),
                }
                for item in hass.config_entries.flow.async_progress()
                if item.get("handler") == DOMAIN
            ]
            _LOGGER.error(
                "Could not resume native auth flow %s: UnknownFlow; "
                "current Microsoft Family flows=%s",
                flow_id,
                in_progress,
            )
            raise web.HTTPBadRequest(text="Authentication flow is no longer active") from err
        except Exception as err:  # Flow may already have been closed/expired.
            _LOGGER.exception(
                "Could not resume native auth flow %s (%s)",
                flow_id,
                type(err).__name__,
            )
            raise web.HTTPBadRequest(text="Authentication flow is no longer active") from err

        result_type = (
            getattr(result.get("type"), "value", result.get("type"))
            if isinstance(result, dict)
            else None
        )
        _LOGGER.debug(
            "Native auth callback HA flow result: flow_id=%s result_type=%s "
            "step_id=%s has_redirect_url=%s",
            flow_id,
            result_type,
            result.get("step_id") if isinstance(result, dict) else None,
            bool(result.get("url")) if isinstance(result, dict) else False,
        )

        # An EXTERNAL_STEP -> EXTERNAL_STEP transition is returned for native
        # mobile OAuth -> web-session capture. Continue in the already-open
        # browser tab; no second popup is required.
        next_url = result.get("url") if isinstance(result, dict) else None
        if next_url:
            _LOGGER.debug(
                "Native Microsoft Family auth callback advancing flow in same browser tab"
            )
            raise web.HTTPFound(location=str(next_url))

        if result_type == "external_done":
            # This is the canonical Home Assistant external-step handoff.
            # async_external_step_done stores the declared next step and the flow
            # manager notifies the frontend because the step id changed. The
            # frontend is then the *only* consumer that advances to finish_proxy.
            # Never call async_configure() a second time here: the frontend may
            # already have consumed EXTERNAL_STEP_DONE, which races the callback
            # and produces UnknownFlow / "flow not found".
            _LOGGER.debug(
                "Native Microsoft Family callback returned external_done; "
                "waiting for Home Assistant frontend to advance to the declared next step"
            )

            # Some browsers miss the first data_entry_flow_progressed event while
            # focus moves between the Microsoft popup and the HA dialog.  Do not
            # configure the flow a second time (that caused the v34 UnknownFlow
            # race).  Instead, re-send the *notification* only while the flow is
            # still parked on this exact EXTERNAL_STEP_DONE next-step id.  HA's
            # frontend GET then remains the sole consumer of the transition.
            async def _renotify_external_done() -> None:
                from homeassistant.data_entry_flow import (
                    EVENT_DATA_ENTRY_FLOW_PROGRESSED,
                    UnknownFlow,
                )

                expected_step = result.get("step_id")
                for delay in (0.75, 2.0, 4.0):
                    await asyncio.sleep(delay)
                    try:
                        current = hass.config_entries.flow.async_get(flow_id)
                    except UnknownFlow:
                        return
                    if current.get("step_id") != expected_step:
                        return
                    _LOGGER.debug(
                        "Re-notifying Home Assistant frontend for pending "
                        "Microsoft Family external_done step %s",
                        expected_step,
                    )
                    hass.bus.async_fire_internal(
                        EVENT_DATA_ENTRY_FLOW_PROGRESSED,
                        {
                            "handler": current.get("handler", "microsoft_family_safety"),
                            "flow_id": flow_id,
                            "refresh": True,
                        },
                    )

            hass.async_create_task(
                _renotify_external_done(),
                "Microsoft Family Safety auth flow frontend re-notify",
            )
        else:
            _LOGGER.info(
                "Native Microsoft Family Home Assistant flow callback returned %s",
                result_type or "unknown",
            )

        _LOGGER.debug(
            "Returning Microsoft Family authentication completion page to browser: "
            "flow_id=%s result_type=%s auto_close=True",
            flow_id, result_type or "unknown",
        )
        return web.Response(
            text=(
                "<!doctype html><html><head><meta charset='utf-8'>"
                "<title>Microsoft Family Safety</title></head><body>"
                "<h2>Authentication completed</h2>"
                "<p>Home Assistant has received the authentication result. "
                "This window can now be closed.</p>"
                "<script>setTimeout(function(){try{window.close();}catch(e){}},300);"
                "setTimeout(function(){var p=document.createElement('p');"
                "p.textContent='If this window did not close automatically, you can close it now.';"
                "document.body.appendChild(p);},1200);</script>"
                "</body></html>"
            ),
            content_type="text/html",
        )


#: Request key holding the original query string of a proxied request, stashed
#: by :func:`_install_security_filter_bypass` before the query is hidden from
#: Home Assistant's security filter.
_ORIGINAL_QUERY_KEY = f"{DOMAIN}_native_auth_original_query"


def _install_security_filter_bypass(hass: HomeAssistant) -> None:
    """Exempt only this integration's proxy routes from HA's security filter.

    Home Assistant installs ``http.security_filter`` as the outermost aiohttp
    middleware. It rejects any request whose *query string* matches a
    file-injection heuristic ``[a-zA-Z0-9_]=/([a-z0-9_.]//?)+``. Microsoft's
    silent Family SSO redirects carry OAuth parameters such as
    ``epctrc=/w/V6cI...`` which match that heuristic, so the browser gets a bare
    ``400 Bad Request`` before the proxy view is ever reached.

    Percent-encoding the value does not help: ``request.query_string`` is the
    already-decoded form, so the filter sees the decoded slashes either way.

    Instead, this middleware runs *before* the security filter and, for proxy
    routes only, hands the downstream chain a request clone whose query string
    is empty. The real query is preserved on the request under
    ``_ORIGINAL_QUERY_KEY`` and read back by :meth:`_target_from_request`.

    Scope is deliberately narrow:
      * only ``AUTH_PROXY_PATH`` requests are affected — never the callback
        (whose ``flow_id`` query is required and never matches the filter), and
        never any other Home Assistant endpoint;
      * the security filter itself is left registered and fully active for the
        rest of the instance.
    """
    if hass.data.get(_FILTER_BYPASS_INSTALLED):
        return
    app = hass.http.app

    @web.middleware
    async def _native_auth_query_bypass(request: web.Request, handler):
        path = request.path
        if not path.startswith(f"{AUTH_PROXY_PATH}/"):
            return await handler(request)
        if not request.query_string:
            return await handler(request)
        original_query = request.query_string
        # Clone with an empty query so the security filter has nothing to match,
        # then carry the real query forward out-of-band.
        stripped = request.clone(rel_url=request.rel_url.with_query(None))
        stripped[_ORIGINAL_QUERY_KEY] = original_query
        return await handler(stripped)

    # Index 0 keeps this outside HA's security filter, which is appended first.
    app.middlewares.insert(0, _native_auth_query_bypass)
    hass.data[_FILTER_BYPASS_INSTALLED] = True
    _LOGGER.debug(
        "Installed Microsoft Family auth proxy security-filter bypass "
        "(scoped to %s/*)",
        AUTH_PROXY_PATH,
    )


def ensure_native_auth_views(hass: HomeAssistant) -> None:
    """Register the shared proxy/callback views once."""
    if hass.data.get(_VIEWS_REGISTERED):
        return
    _install_security_filter_bypass(hass)
    hass.http.register_view(MicrosoftFamilyAuthorizationProxyView())
    hass.http.register_view(MicrosoftFamilyAuthorizationCallbackView())
    hass.data[_VIEWS_REGISTERED] = True
    hass.data.setdefault(_PROXY_REGISTRY, {})


def register_native_proxy(hass: HomeAssistant, proxy: MicrosoftFamilyAuthProxy) -> None:
    ensure_native_auth_views(hass)
    hass.data.setdefault(_PROXY_REGISTRY, {})[proxy.token] = proxy

    async def expire() -> None:
        await asyncio.sleep(600)
        registry = hass.data.get(_PROXY_REGISTRY, {})
        if registry.pop(proxy.token, None) is proxy:
            _LOGGER.debug("Expired native Family auth proxy flow")
        await proxy.async_close()

    proxy._expiry_task = hass.async_create_task(expire())


async def unregister_native_proxy(hass: HomeAssistant, proxy: MicrosoftFamilyAuthProxy) -> None:
    registry = hass.data.get(_PROXY_REGISTRY, {})
    registry.pop(proxy.token, None)
    await proxy.async_close()
