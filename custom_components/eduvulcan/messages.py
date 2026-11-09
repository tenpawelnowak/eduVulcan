"""Fire Home Assistant events for new eduVULCAN messages and announcements.

Local addition (not upstream): eduvulcan_message / eduvulcan_announcement events.

Messages come from the web module (wiadomosci.eduvulcan.pl) when an eduVULCAN
e-mail and password are set in the integration options; the mobile API only
serves them with eduVULCAN Premium. Announcements still come from the mobile API.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import CONF_PASSWORD, CONF_USERNAME, DOMAIN
from .iris._exceptions import HttpUnsuccessfullStatusException, IrisApiException
from .iris.api import IrisHebeCeApi
from .iris.models import Account
from .web_messages import EduVulcanWebClient, WebAuthError, WebCaptchaRequired, WebMessagesError

_LOGGER = logging.getLogger(__name__)

EVENT_MESSAGE = "eduvulcan_message"
EVENT_ANNOUNCEMENT = "eduvulcan_announcement"
STORAGE_VERSION = 1
SYNC_DAYS = 30  # only ask Vulcan for items changed in the last 30 days
MAX_SEEN = 1000
WEB_DATA = f"{DOMAIN}_web"
ISSUE_WEB_LOGIN = "web_login"
WEB_ERRORS = (WebMessagesError, aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError)

_TAG_BREAK = re.compile(r"<\s*(br|/p|/div|/li)\s*/?>", re.I)
_TAG = re.compile(r"<[^>]+>")


def html_to_text(value: str | None) -> str:
    """Message bodies are HTML; Telegram gets plain text."""
    text = html.unescape(_TAG.sub("", _TAG_BREAK.sub("\n", value or "")))
    lines = [line.strip() for line in text.replace("\xa0", " ").splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt_util.get_default_time_zone())
    return value.isoformat()


def async_get_web_client(hass: HomeAssistant) -> EduVulcanWebClient | None:
    """The shared web client, built from the first entry that has web credentials.

    All pupils of one eduVULCAN account share a login, so it is enough to set the
    e-mail and password in one of the entries.
    """
    creds = next(
        (
            (entry.options[CONF_USERNAME], entry.options[CONF_PASSWORD])
            for entry in hass.config_entries.async_entries(DOMAIN)
            if entry.options.get(CONF_USERNAME) and entry.options.get(CONF_PASSWORD)
        ),
        None,
    )
    clients: dict[tuple[str, str], EduVulcanWebClient] = hass.data.setdefault(WEB_DATA, {})
    if creds not in clients:
        for old in clients.values():
            hass.async_create_task(old.async_close())
        clients.clear()
        if creds is None:
            ir.async_delete_issue(hass, DOMAIN, ISSUE_WEB_LOGIN)
            return None
        clients[creds] = EduVulcanWebClient(hass, *creds)
    return clients[creds]


class EduVulcanMessages:
    """Remembers which messages and announcements were seen, per config entry.

    The first check of each kind only records what exists. Items already fired by
    another entry (siblings can get the same message) are recorded but not fired twice.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        api: IrisHebeCeApi,
        tenant: str,
        web: Callable[[], EduVulcanWebClient | None],
    ) -> None:
        self._hass = hass
        self._api = api
        self._tenant = tenant
        self._web = web
        self._store: Store[dict[str, list[str]]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}.seen"
        )
        self._seen: dict[str, list[str]] | None = None
        self._fired: set[str] = hass.data.setdefault(f"{DOMAIN}_fired", set())

    # --- messages ------------------------------------------------------------------

    async def _mobile_messages(self, account: Account) -> list[dict[str, Any]]:
        if not account.message_box:
            return []
        since = datetime.now() - timedelta(days=SYNC_DAYS)
        try:
            received = await self._api.get_received_messages(
                rest_url=account.unit.rest_url,
                box=account.message_box.global_key,
                pupil_id=account.pupil.id,
                last_sync_date=since,
            )
        except IrisApiException as err:
            if "EDUVULCAN_PREMIUM" not in str(err):
                raise
            _LOGGER.debug(
                "eduVULCAN mobile messages need Premium; set e-mail and password in "
                "the integration options to read them from the web module"
            )
            return []
        return [
            {
                "kid": account.pupil.first_name,
                "id": m.global_key,
                "subject": m.subject,
                "text": html_to_text(m.content),
                "sender": m.sender.name,
                "attachments": len(m.attachments),
                "date": _iso(m.sent_at),
                "test": False,
            }
            for m in received
            if not m.widthdrawn
        ]

    async def _web_messages(
        self, web: EduVulcanWebClient, account: Account
    ) -> list[dict[str, Any]]:
        """Message list without bodies; bodies are fetched only for new items."""
        pupil = account.pupil
        box = f"{pupil.surname} {pupil.first_name}".casefold()
        return [
            {
                "kid": pupil.first_name,
                "id": m["apiGlobalKey"],
                "subject": m.get("temat"),
                "text": None,
                "sender": m.get("korespondenci"),
                "attachments": int(bool(m.get("hasZalaczniki"))),
                "date": m.get("data"),
                "test": False,
            }
            for m in await web.async_received(self._tenant, f"{pupil.first_name} {pupil.surname}")
            if not m.get("wycofana") and box in (m.get("skrzynka") or "").casefold()
        ]

    async def _with_body(self, item: dict[str, Any], account: Account) -> dict[str, Any]:
        """Fill in the web message body. Vulcan may mark the message as read."""
        web = self._web()
        if item["text"] is not None or web is None:
            return item
        pupil = account.pupil
        details = await web.async_details(
            self._tenant, f"{pupil.first_name} {pupil.surname}", item["id"]
        )
        return {
            **item,
            "text": html_to_text(details.get("tresc")),
            "sender": details.get("nadawca") or item["sender"],
            "attachments": len(details.get("zalaczniki") or []),
        }

    async def _messages(self, account: Account) -> tuple[str, list[dict[str, Any]]]:
        """(seen-list key, messages). Web items use their own key so that switching
        sources records the existing ones instead of re-sending them."""
        web = self._web()
        if web is None:
            return "messages", await self._mobile_messages(account)
        try:
            items = await self._web_messages(web, account)
        except WebAuthError as err:
            ir.async_create_issue(
                self._hass,
                DOMAIN,
                ISSUE_WEB_LOGIN,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="web_captcha" if isinstance(err, WebCaptchaRequired) else "web_auth",
            )
            raise
        ir.async_delete_issue(self._hass, DOMAIN, ISSUE_WEB_LOGIN)
        return "web_messages", items

    # --- announcements ----------------------------------------------------------------

    async def _announcements(self, account: Account) -> list[dict[str, Any]]:
        since = datetime.now() - timedelta(days=SYNC_DAYS)
        try:
            raw = await self._api.get_announcements(
                rest_url=account.unit.rest_url, pupil_id=account.pupil.id, last_sync_date=since
            )
        except (HttpUnsuccessfullStatusException, IrisApiException) as err:
            # Some schools don't publish announcements (404); Premium may block them too.
            _LOGGER.debug("eduVULCAN announcements unavailable for %s: %s", account.pupil.first_name, err)
            return []
        return [
            {
                "kid": account.pupil.first_name,
                "id": f"{a.unit_id}:{a.id}",
                "title": a.title,
                "text": html_to_text(a.content),
                "sender": a.sender.display_name,
                "category": a.category,
                "valid_from": a.date_from.isoformat(),
                "valid_to": a.date_to.isoformat(),
                "attachments": len(a.attachments),
                "date": _iso(a.created_at),
                "test": False,
            }
            for a in raw
        ]

    # --- public ----------------------------------------------------------------------

    async def async_check(self, accounts: list[Account]) -> None:
        if self._seen is None:
            self._seen = await self._store.async_load() or {}
        events: list[tuple[str, dict[str, Any]]] = []
        for account in accounts:
            batches: list[tuple[str, str, list[dict[str, Any]]]] = []
            try:
                key, messages = await self._messages(account)
                batches.append((key, EVENT_MESSAGE, messages))
            except WEB_ERRORS as err:
                _LOGGER.warning("eduVULCAN web messages failed for %s: %s", account.pupil.first_name, err)
            batches.append(("announcements", EVENT_ANNOUNCEMENT, await self._announcements(account)))

            for key, event_type, items in batches:
                first_run = key not in self._seen
                seen = self._seen.setdefault(key, [])
                for item in sorted(items, key=lambda i: i["date"] or ""):
                    if item["id"] in seen:
                        continue
                    # The same message to two siblings has two ids on the web.
                    same = f"{item.get('subject') or item.get('title')}|{(item['date'] or '')[:16]}|{item['sender']}"
                    if not first_run and item["id"] not in self._fired and same not in self._fired:
                        try:
                            item = await self._with_body(item, account)
                        except WEB_ERRORS as err:
                            # Not marked as seen: retried on the next refresh.
                            _LOGGER.warning("eduVULCAN message body failed: %s", err)
                            continue
                        self._fired.update((item["id"], same))
                        events.append((event_type, item))
                    seen.append(item["id"])
                del seen[:-MAX_SEEN]
        await self._store.async_save(self._seen)
        for event_type, data in events:
            self._hass.bus.async_fire(event_type, data)

    async def async_latest(self, accounts: list[Account]) -> dict[str, dict[str, Any]]:
        """Newest message and newest announcement across the pupils of this entry."""
        latest: dict[str, tuple[dict[str, Any], Account]] = {}
        for account in accounts:
            _, messages = await self._messages(account)
            for event_type, items in (
                (EVENT_MESSAGE, messages),
                (EVENT_ANNOUNCEMENT, await self._announcements(account)),
            ):
                for item in items:
                    if (item["date"] or "") > (latest.get(event_type, ({}, None))[0].get("date") or ""):
                        latest[event_type] = (item, account)
        return {
            event_type: await self._with_body(item, account)
            for event_type, (item, account) in latest.items()
        }
