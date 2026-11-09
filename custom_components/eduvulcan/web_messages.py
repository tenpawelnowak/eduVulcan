"""Messages from the eduVULCAN web module (wiadomosci.eduvulcan.pl).

Since November 2026 the mobile (hebeCE) API answers message requests with
``111: EDUVULCAN_PREMIUM``. The web module the parents use in the browser still
works without Premium, so we log in the same way the browser does:

1. ``eduvulcan.pl/logowanie`` with the eduVULCAN e-mail and password,
2. ``eduvulcan.pl/dostep-do-dziennika/`` lists one link per pupil (the link key
   changes on every page load),
3. opening a pupil's link starts a journal session in that pupil's unit
   (tenant), after which ``wiadomosci.eduvulcan.pl/<tenant>/api/...`` answers.

Only one unit can be active per session: entering Kraków ends the Wieliczka
session. All calls therefore go through one lock and one unit at a time.

If Vulcan asks for a captcha we stop and report it; we never try to solve it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlsplit

import aiohttp
from yarl import URL

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession

_LOGGER = logging.getLogger(__name__)

LOGIN_URL = "https://eduvulcan.pl/logowanie"
QUERY_USER_URL = "https://eduvulcan.pl/Account/QueryUserInfo"
ACCESS_URL = "https://eduvulcan.pl/dostep-do-dziennika/"
WEB_BASE = "https://wiadomosci.eduvulcan.pl"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
)
# Hosts whose self-submitting SSO forms we are willing to post on.
TRUSTED_DOMAINS = ("eduvulcan.pl", "vulcan.net.pl")
# Fields of WS-Federation / SAML / OIDC form_post responses. Other hidden-only
# forms (e.g. logout) are never posted.
SSO_FIELDS = {"wa", "wresult", "wctx", "SAMLResponse", "RelayState", "id_token", "code", "state"}
LIST_CACHE_SECONDS = 120
PAGE_SIZE = 50
CAPTCHA_BACKOFF = 6 * 3600
MAX_REDIRECTS = 15


class WebMessagesError(Exception):
    """The web module did not answer as expected."""


class WebAuthError(WebMessagesError):
    """Login rejected (wrong e-mail or password)."""


class WebCaptchaRequired(WebAuthError):
    """Vulcan wants a captcha; the user has to log in in a browser first."""


class _SessionLost(Exception):
    """The current unit session is gone (expired or another unit was entered)."""


@dataclass
class _Form:
    action: str
    method: str
    inputs: dict[str, str] = field(default_factory=dict)
    types: set[str] = field(default_factory=set)


class _PageParser(HTMLParser):
    """Collects forms (with inputs) and links (with their visible text)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[_Form] = []
        self.links: list[tuple[str, str]] = []
        self._link: list[str] | None = None
        self._href = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: v or "" for k, v in attrs}
        if tag == "form":
            self.forms.append(_Form(a.get("action", ""), a.get("method", "get").lower()))
        elif tag == "input" and self.forms:
            form = self.forms[-1]
            form.types.add(a.get("type", "text").lower())
            if a.get("name"):
                form.inputs[a["name"]] = a.get("value", "")
        elif tag == "a" and a.get("href"):
            self._href, self._link = a["href"], []

    def handle_data(self, data: str) -> None:
        if self._link is not None:
            self._link.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._link is not None:
            self.links.append((self._href, " ".join("".join(self._link).split())))
            self._link = None


def _parse(text: str) -> _PageParser:
    parser = _PageParser()
    parser.feed(text)
    return parser


def _trusted(url: str) -> bool:
    host = urlsplit(url).hostname or ""
    return any(host == d or host.endswith("." + d) for d in TRUSTED_DOMAINS)


def _where(url: Any) -> str:
    """Host and path only, for logs (query strings carry session keys)."""
    parts = urlsplit(str(url))
    return f"{parts.hostname}{parts.path}"


class EduVulcanWebClient:
    """One logged-in browser-like session shared by all eduVULCAN entries."""

    def __init__(self, hass: HomeAssistant, username: str, password: str) -> None:
        self._username = username
        self._password = password
        # Vulcan session cookies contain "+/=:"; aiohttp would send them quoted
        # ("...") and the login server then answers 404.
        self._session: aiohttp.ClientSession = async_create_clientsession(
            hass, cookie_jar=aiohttp.CookieJar(quote_cookie=False)
        )
        self._lock = asyncio.Lock()
        self._tenant: str | None = None
        self._cache: dict[tuple[str, str], tuple[float, list[dict[str, Any]]]] = {}
        # After a rejected login don't retry until the password changes (a new
        # client); after a captcha wait a few hours. Hammering may lock the account.
        self._auth_error: WebAuthError | None = None
        self._retry_at = 0.0

    async def async_close(self) -> None:
        await self._session.close()

    # --- low level -----------------------------------------------------------------

    async def _request(
        self, method: str, url: str, data: dict[str, str] | None = None
    ) -> tuple[str, str]:
        """Follow redirects ourselves, passing each Location on unchanged like a
        browser does (aiohttp would re-quote ``wtrealm=https%3A%2F%2F...``).

        Home Assistant replaces the session User-Agent, so the browser one is
        set on every request.
        """
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "pl-PL,pl;q=0.9",
        }
        target = URL(url, encoded=True)
        for _ in range(MAX_REDIRECTS):
            async with self._session.request(
                method, target, data=data, headers=headers, allow_redirects=False
            ) as resp:
                location = resp.headers.get("Location")
                if resp.status not in (301, 302, 303, 307, 308) or not location:
                    return str(resp.url), await resp.text()
            target = URL(urljoin(str(target), location), encoded=True)
            if resp.status in (301, 302, 303):
                method, data = "GET", None
        raise WebMessagesError(f"too many redirects at {_where(target)}")

    async def _get(self, url: str) -> tuple[str, str]:
        return await self._follow_sso_forms(*await self._request("GET", url))

    async def _follow_sso_forms(self, url: str, text: str) -> tuple[str, str]:
        """Post self-submitting SSO forms (only hidden inputs) like a browser would."""
        for _ in range(5):
            forms = [
                f
                for f in _parse(text).forms
                if f.method == "post"
                and f.types <= {"hidden", "submit"}
                and SSO_FIELDS & f.inputs.keys()
            ]
            if not forms:
                break
            target = urljoin(url, forms[0].action or url)
            if not _trusted(target):
                break
            _LOGGER.debug("eduVULCAN web: SSO form %s -> %s", _where(url), _where(target))
            url, text = await self._request("POST", target, forms[0].inputs)
        return url, text

    # --- login and unit selection ----------------------------------------------------

    async def _login(self) -> None:
        if self._auth_error and time.monotonic() < self._retry_at:
            raise self._auth_error
        try:
            await self._do_login()
        except WebCaptchaRequired as err:
            self._auth_error, self._retry_at = err, time.monotonic() + CAPTCHA_BACKOFF
            raise
        except WebAuthError as err:
            self._auth_error, self._retry_at = err, float("inf")
            raise
        self._auth_error = None

    async def _do_login(self) -> None:
        _LOGGER.debug("eduVULCAN web: logging in")
        url, text = await self._get(LOGIN_URL)
        form = next((f for f in _parse(text).forms if "UserName" in f.inputs), None)
        if form is None:
            raise WebMessagesError(f"login form not found at {_where(url)}")
        token = form.inputs.get("__RequestVerificationToken", "")

        # Same pre-check the login page makes after the e-mail step.
        _, body = await self._request(
            "POST", QUERY_USER_URL, {"alias": self._username, "__RequestVerificationToken": token}
        )
        info = json.loads(body)
        if not info.get("success") or (info.get("data") or {}).get("ShowCaptcha"):
            raise WebCaptchaRequired("eduVULCAN asks for a captcha")

        data = {
            "UserName": self._username,
            "Password": self._password,
            "captcha-response": "",
            "__RequestVerificationToken": token,
        }
        url, text = await self._follow_sso_forms(
            *await self._request("POST", urljoin(url, form.action or url), data)
        )
        if urlsplit(url).path.rstrip("/").endswith("/logowanie"):
            raise WebAuthError("eduVULCAN rejected the e-mail or password")
        _LOGGER.debug("eduVULCAN web: logged in, landed on %s", _where(url))

    async def _journal_links(self) -> list[tuple[str, str]]:
        for attempt in (0, 1):
            url, text = await self._get(ACCESS_URL)
            links = [
                (urljoin(url, href), label)
                for href, label in _parse(text).links
                if "/dziennik?" in href
            ]
            if links:
                return links
            if attempt == 0:
                await self._login()
        raise WebMessagesError(f"no pupils listed at {_where(url)}")

    async def _enter(self, tenant: str, pupil: str) -> None:
        """Open the pupil's journal link, then the web messages module of its unit."""
        self._tenant = None
        links = await self._journal_links()
        wanted = pupil.casefold().split()
        link = next(
            (href for href, label in links if all(w in label.casefold() for w in wanted)),
            None,
        )
        if link is None:
            names = ", ".join(label for _, label in links)
            raise WebMessagesError(f"pupil {pupil!r} not on the journal list ({names})")
        url, _ = await self._get(link)
        _LOGGER.debug("eduVULCAN web: journal link for %s -> %s", pupil, _where(url))
        url, _ = await self._get(f"{WEB_BASE}/{tenant}/App")
        if not url.startswith(f"{WEB_BASE}/{tenant}/"):
            raise WebMessagesError(f"messages module for {tenant} redirected to {_where(url)}")
        self._tenant = tenant

    async def _api(self, tenant: str, endpoint: str, params: dict[str, Any]) -> Any:
        async with self._session.get(
            f"{WEB_BASE}/{tenant}/api/{endpoint}",
            params=params,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
                "X-Requested-With": "XMLHttpRequest",
            },
            allow_redirects=False,
        ) as resp:
            if resp.status != 200 or "json" not in resp.content_type:
                raise _SessionLost(f"{endpoint}: HTTP {resp.status} {resp.content_type}")
            return await resp.json()

    async def _call(self, tenant: str, pupil: str, endpoint: str, params: dict[str, Any]) -> Any:
        """API call in the pupil's unit; re-enters (and re-logs in) once if needed."""
        for attempt in (0, 1):
            try:
                if self._tenant != tenant:
                    await self._enter(tenant, pupil)
                return await self._api(tenant, endpoint, params)
            except _SessionLost as err:
                _LOGGER.debug("eduVULCAN web: session lost (%s), attempt %s", err, attempt)
                self._tenant = None
                if attempt:
                    raise WebMessagesError(str(err)) from err
        raise AssertionError("unreachable")

    # --- public --------------------------------------------------------------------

    async def async_received(self, tenant: str, pupil: str) -> list[dict[str, Any]]:
        """Newest received messages of the unit (all boxes of the account in that unit)."""
        async with self._lock:
            # Pupils in the same unit share one list; don't switch units twice in a row.
            cached = self._cache.get((tenant, "list"))
            if cached and time.monotonic() - cached[0] < LIST_CACHE_SECONDS:
                return cached[1]
            items = await self._call(
                tenant, pupil, "Odebrane", {"idLastWiadomosc": 0, "pageSize": PAGE_SIZE}
            )
            self._cache[(tenant, "list")] = (time.monotonic(), items)
            return items

    async def async_details(self, tenant: str, pupil: str, key: str) -> dict[str, Any]:
        """Full message. NOTE: Vulcan may mark the message as read when this is called."""
        async with self._lock:
            return await self._call(tenant, pupil, "WiadomoscSzczegoly", {"apiGlobalKey": key})

