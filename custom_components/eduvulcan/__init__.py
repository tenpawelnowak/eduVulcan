"""The eduVULCAN integration.

Pulls kids' timetable, homework, exams and the lucky number from the
eduVULCAN (hebeCE) mobile API via a vendored copy of the `iris` library.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import CONF_CREDENTIAL, DOMAIN
from .coordinator import EduVulcanCoordinator
from .iris.api import IrisHebeCeApi
from .iris.credentials import RsaCredential

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]

type EduVulcanConfigEntry = ConfigEntry[EduVulcanCoordinator]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register eduvulcan.send_latest: fires the newest message and announcement again (test: true)."""

    async def send_latest(call: ServiceCall) -> None:
        latest: dict[str, dict] = {}
        for entry in hass.config_entries.async_entries(DOMAIN):
            if entry.state is not ConfigEntryState.LOADED or not entry.runtime_data.data:
                continue
            coordinator: EduVulcanCoordinator = entry.runtime_data
            found = await coordinator.messages.async_latest(
                [p.account for p in coordinator.data.values()]
            )
            for event_type, item in found.items():
                if (item["date"] or "") > (latest.get(event_type, {}).get("date") or ""):
                    latest[event_type] = item
        for event_type, item in latest.items():
            hass.bus.async_fire(event_type, {**item, "test": True})

    hass.services.async_register(DOMAIN, "send_latest", send_latest)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: EduVulcanConfigEntry) -> bool:
    """Set up eduVULCAN from a config entry."""
    credential = RsaCredential.model_validate_json(entry.data[CONF_CREDENTIAL])
    api = IrisHebeCeApi(credential)

    coordinator = EduVulcanCoordinator(hass, entry, api)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: EduVulcanConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.async_close()
    return unload_ok
