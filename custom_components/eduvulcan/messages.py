"""Fire Home Assistant events for new eduVULCAN messages and announcements.

Events: eduvulcan_message and eduvulcan_announcement (see README).
"""

from __future__ import annotations

import html
import logging
import re
from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .iris._exceptions import HttpUnsuccessfullStatusException
from .iris.api import IrisHebeCeApi
from .iris.models import Account

_LOGGER = logging.getLogger(__name__)

EVENT_MESSAGE = "eduvulcan_message"
EVENT_ANNOUNCEMENT = "eduvulcan_announcement"
STORAGE_VERSION = 1
SYNC_DAYS = 30  # only ask Vulcan for items changed in the last 30 days
MAX_SEEN = 1000

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


class EduVulcanMessages:
    """Remembers which messages and announcements were seen, per config entry.

    The first check only records what exists. Items already fired by another entry
    (siblings can share a message box) are recorded but not fired twice.
    """

    def __init__(self, hass: HomeAssistant, entry_id: str, api: IrisHebeCeApi) -> None:
        self._hass = hass
        self._api = api
        self._store: Store[dict[str, list[str]]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}.seen"
        )
        self._seen: dict[str, list[str]] | None = None
        self._fired: set[str] = hass.data.setdefault(f"{DOMAIN}_fired", set())

    async def _fetch(self, account: Account) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        since = datetime.now() - timedelta(days=SYNC_DAYS)
        rest_url, pupil_id = account.unit.rest_url, account.pupil.id
        kid = account.pupil.first_name
        messages: list[dict[str, Any]] = []
        if account.message_box:
            for m in await self._api.get_received_messages(
                rest_url=rest_url,
                box=account.message_box.global_key,
                pupil_id=pupil_id,
                last_sync_date=since,
            ):
                if m.widthdrawn:
                    continue
                messages.append(
                    {
                        "kid": kid,
                        "id": m.global_key,
                        "subject": m.subject,
                        "text": html_to_text(m.content),
                        "sender": m.sender.name,
                        "attachments": len(m.attachments),
                        "date": _iso(m.sent_at),
                        "test": False,
                    }
                )
        try:
            raw_announcements = await self._api.get_announcements(
                rest_url=rest_url, pupil_id=pupil_id, last_sync_date=since
            )
        except HttpUnsuccessfullStatusException as err:
            # Some schools don't publish announcements; the endpoint then returns 404.
            _LOGGER.debug("eduVULCAN announcements unavailable for %s: %s", kid, err)
            raw_announcements = []
        announcements = [
            {
                "kid": kid,
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
            for a in raw_announcements
        ]
        return messages, announcements

    async def async_check(self, accounts: list[Account]) -> None:
        if self._seen is None:
            self._seen = await self._store.async_load() or {}
        first_run = not self._seen
        events: list[tuple[str, dict[str, Any]]] = []
        for account in accounts:
            messages, announcements = await self._fetch(account)
            for key, event_type, items in (
                ("messages", EVENT_MESSAGE, messages),
                ("announcements", EVENT_ANNOUNCEMENT, announcements),
            ):
                seen = self._seen.setdefault(key, [])
                for item in sorted(items, key=lambda i: i["date"] or ""):
                    if item["id"] in seen:
                        continue
                    seen.append(item["id"])
                    if not first_run and item["id"] not in self._fired:
                        self._fired.add(item["id"])
                        events.append((event_type, item))
                del seen[:-MAX_SEEN]
        await self._store.async_save(self._seen)
        for event_type, data in events:
            self._hass.bus.async_fire(event_type, data)

    async def async_latest(self, accounts: list[Account]) -> dict[str, dict[str, Any]]:
        """Newest message and newest announcement across the pupils of this entry."""
        latest: dict[str, dict[str, Any]] = {}
        for account in accounts:
            messages, announcements = await self._fetch(account)
            for event_type, items in ((EVENT_MESSAGE, messages), (EVENT_ANNOUNCEMENT, announcements)):
                for item in items:
                    if (item["date"] or "") > (latest.get(event_type, {}).get("date") or ""):
                        latest[event_type] = item
        return latest
