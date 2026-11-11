"""sync_site_list: keep a child's blocked websites in step with a published list.

The list is plain text, one domain per line, ``#`` comments allowed (hosts-file lines such as
``0.0.0.0 example.com`` are tolerated). A sync

* adds the listed domains that are not blocked yet;
* removes only domains this integration added in an earlier sync of the same
  list for the same child and that the list has since dropped (tracked in a
  Store), so sites blocked by hand and allowed sites are never touched;
* leaves out a domain that shares a site with one on the allowed list.

The list comes from a third party and is downloaded from inside Home
Assistant, so the service is for administrators only and holds the download
to https, no redirects, a size cap and plain domains. An empty or failed
download, one that would take back most of what the list added (a list cut
short), or a read of Microsoft's lists that has lost every site the list
added, changes nothing.

Everything goes through the mobile API: the current lists are read with
``GET /v1/WebRestrictions/{child}?exceptions=true``
(api_client.get_web_restrictions) and written with the same PATCH as the
block_website and remove_website actions.

Microsoft's ``PATCH /v1/WebRestrictions/{child}`` body is a list of
operations, but only one operation per request has ever been captured
(api_client.block_website), so each site is still its own request. They run
a few at a time; a failed one is reported and, since only successes are
recorded, retried by the next sync.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any
from urllib.parse import urlsplit

import voluptuous as vol

from homeassistant.helpers import config_validation as cv

# One sync must not push an unbounded list.
MAX_SYNC_DOMAINS = 2000
# A list is read up to this size; MAX_SYNC_DOMAINS domains is far smaller.
MAX_LIST_BYTES = 1024 * 1024
# A sync removes at most half of what its list added, or this many if that is more.
SYNC_REMOVE_FLOOR = 20
# Requests in flight at once: Microsoft dropped connections when schedule
# writes were sent all at once, so stay modest.
WRITE_CONCURRENCY = 4
# Responses list at most this many domains; the counts are always complete.
RESPONSE_SAMPLE = 10
DOWNLOAD_TIMEOUT = 60

_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_HOST = re.compile(rf"^(?=.{{1,253}}$){_LABEL}(?:\.{_LABEL})+$")
_IPV4 = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
# A second-level public suffix (co.uk, com.au, ...): blocking one would block
# a whole country's sites. Best effort, there is no public suffix list here.
_SUFFIX = re.compile(r"^(?:ac|co|com|edu|gov|net|or|org)\.[a-z]{2}$")


def https_url(value: Any) -> str:
    """The list URL; the list is a third party's, so only over https."""
    url = cv.url(value)
    if urlsplit(url).scheme != "https":
        raise vol.Invalid("The site list URL must start with https://")
    return url


def parse_site_list(text: str, ignored: list[str] | None = None) -> list[str]:
    """Plain domains from a one-per-line list; anything else goes to ``ignored``."""
    domains: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        body = line.split("#", 1)[0].strip()
        if not body:
            continue
        token = body.split()[-1].lower().rstrip(".")  # "0.0.0.0 example.com"
        if token in seen:
            continue
        seen.add(token)
        if _HOST.match(token) and not _IPV4.match(token) and not _SUFFIX.match(token):
            domains.append(token)
        elif ignored is not None:
            ignored.append(token)
    return domains


def site_host(value: Any) -> str | None:
    """"https://www.example.com/path" -> "www.example.com"; None if empty."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if "://" in text:
        text = text.split("://", 1)[1]
    host = text.split("/", 1)[0].split(":", 1)[0].lower().rstrip(".")
    return host or None


def site_lists(web_data: dict[str, Any] | None) -> tuple[list[str], list[str]] | None:
    """(blocked, allowed) host names from Microsoft's web restrictions; None if unreadable.

    From ``exceptions`` (``{"website", "allowed"}``), else from the
    ``blockedSites`` / ``allowedSites`` lists the read derives.
    """
    if not isinstance(web_data, dict):
        return None
    blocked: list[str] = []
    allowed: list[str] = []
    exceptions = web_data.get("exceptions", web_data.get("Exceptions"))
    if isinstance(exceptions, list) and exceptions:
        for item in exceptions:
            if not isinstance(item, dict):
                continue
            host = site_host(item.get("website") or item.get("Website"))
            if host:
                (allowed if item.get("allowed", item.get("Allowed")) else blocked).append(host)
    elif isinstance(exceptions, list) or "blockedSites" in web_data or "allowedSites" in web_data:
        blocked = [h for h in map(site_host, web_data.get("blockedSites") or []) if h]
        allowed = [h for h in map(site_host, web_data.get("allowedSites") or []) if h]
    else:
        return None
    return sorted(dict.fromkeys(blocked)), sorted(dict.fromkeys(allowed))


def stored_site_lists(web_data: dict[str, Any] | None) -> tuple[list[str], list[str]] | None:
    """(blocked, allowed) exactly as Microsoft stores them (absolute URLs); None if unreadable."""
    if not isinstance(web_data, dict):
        return None
    exceptions = web_data.get("exceptions", web_data.get("Exceptions"))
    if isinstance(exceptions, list) and exceptions:
        blocked: list[str] = []
        allowed: list[str] = []
        for item in exceptions:
            if not isinstance(item, dict):
                continue
            site = item.get("website") or item.get("Website")
            if isinstance(site, str) and site:
                (allowed if item.get("allowed", item.get("Allowed")) else blocked).append(site)
        return blocked, allowed
    if isinstance(exceptions, list) or "blockedSites" in web_data or "allowedSites" in web_data:
        return (
            [s for s in web_data.get("blockedSites") or [] if isinstance(s, str)],
            [s for s in web_data.get("allowedSites") or [] if isinstance(s, str)],
        )
    return None


def _within(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def allowed_overlaps(allowed_host: str, domain: str) -> bool:
    """Whether blocking ``domain`` would hit a site the parent allowed.

    Blocking example.com while www.example.com is allowed (or the other way
    round) would block the allowed site, so such a domain is left alone.
    """
    allowed_host = allowed_host.removeprefix("*.")
    return _within(allowed_host, domain) or _within(domain, allowed_host)


def sample(items: list[str]) -> dict[str, Any]:
    """A response entry: the full count and a few examples."""
    return {"count": len(items), "examples": items[:RESPONSE_SAMPLE]}


async def run_bounded(
    sites: list[str], write, concurrency: int = WRITE_CONCURRENCY
) -> tuple[list[str], dict[str, str]]:
    """Call ``write(site)`` for each site, a few at a time.

    Returns the sites that succeeded and site -> error for those that failed;
    one failure never stops the others.
    """
    pending = iter(sites)
    done: list[str] = []
    failed: dict[str, str] = {}

    async def worker() -> None:
        # A few workers sharing one iterator, rather than one task per site.
        for site in pending:
            try:
                await write(site)
            except Exception as err:  # noqa: BLE001 - reported, retried next sync
                failed[site] = str(err)[:200]
            else:
                done.append(site)

    await asyncio.gather(*(worker() for _ in range(max(1, min(concurrency, len(sites))))))
    order = {site: index for index, site in enumerate(sites)}
    return sorted(done, key=order.__getitem__), failed
