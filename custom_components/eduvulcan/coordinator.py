"""Data update coordinator for the eduVULCAN integration."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from pydantic import ValidationError

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    EXAMS_DAYS_AHEAD,
    HOMEWORK_DAYS_AHEAD,
    SCHEDULE_DAYS_AHEAD,
)
from .iris._exceptions import IrisApiException
from .iris.api import IrisHebeCeApi
from .iris.models import Account, Exam, Homework, Schedule
from .messages import EduVulcanMessages

_LOGGER = logging.getLogger(__name__)


@dataclass
class PupilData:
    """Snapshot of one pupil's school data."""

    account: Account
    schedule: list[Schedule] = field(default_factory=list)
    # Raw entries from mobile/schedule/changes/byPupil keyed by ScheduleId —
    # eduVULCAN publishes substitutions/cancellations THERE, not embedded in
    # the withchanges schedule (verified live 2026-09-21).
    schedule_changes: dict[int, dict] = field(default_factory=dict)
    homework: list[Homework] = field(default_factory=list)
    exams: list[Exam] = field(default_factory=list)
    lucky_number: int | None = None


class EduVulcanCoordinator(DataUpdateCoordinator[dict[int, PupilData]]):
    """Fetches data for all pupils on the registered eduVULCAN account."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        api: IrisHebeCeApi,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=entry,
            update_interval=DEFAULT_UPDATE_INTERVAL,
        )
        self.api = api
        self.messages = EduVulcanMessages(hass, entry.entry_id, api)

    async def async_close(self) -> None:
        """Release the underlying aiohttp session."""
        await self.api._http.close()  # noqa: SLF001 (vendored lib has no public close)

    async def _async_update_data(self) -> dict[int, PupilData]:
        today = date.today()
        try:
            accounts = await self.api.get_accounts()
        except IrisApiException as err:
            raise UpdateFailed(f"eduVULCAN: cannot fetch account list: {err}") from err

        data: dict[int, PupilData] = {}
        for account in accounts:
            pupil_id = account.pupil.id
            rest_url = account.unit.rest_url
            pupil_data = PupilData(account=account)

            try:
                # Raw fetch (same endpoint get_schedule uses) so we can both
                # build the models AND debug-log entries carrying change info.
                envelope = await self.api._http.request(  # noqa: SLF001
                    method="GET",
                    rest_url=rest_url,
                    pupil_id=pupil_id,
                    endpoint="mobile/schedule/withchanges/byPupil",
                    query={
                        "pupilId": pupil_id,
                        "dateFrom": today,
                        "dateTo": today + timedelta(days=SCHEDULE_DAYS_AHEAD),
                        "lastId": -2_147_483_648,
                        "pageSize": 500,
                        "lastSyncDate": datetime(1970, 1, 1, 1, 0, 0),
                    },
                ) or []
                pupil_data.schedule = [
                    Schedule.model_validate(entry) for entry in envelope
                ]

                # Substitutions/cancellations live in a SEPARATE endpoint
                # (mobile/schedule/changes/byPupil), linked via ScheduleId.
                changes_envelope = await self.api._http.request(  # noqa: SLF001
                    method="GET",
                    rest_url=rest_url,
                    pupil_id=pupil_id,
                    endpoint="mobile/schedule/changes/byPupil",
                    query={
                        "pupilId": pupil_id,
                        "dateFrom": today,
                        "dateTo": today + timedelta(days=SCHEDULE_DAYS_AHEAD),
                        "lastId": -2_147_483_648,
                        "pageSize": 500,
                        "lastSyncDate": datetime(1970, 1, 1, 1, 0, 0),
                    },
                ) or []
                pupil_data.schedule_changes = {
                    change["ScheduleId"]: change
                    for change in changes_envelope
                    if isinstance(change, dict) and change.get("ScheduleId") is not None
                }
                if _LOGGER.isEnabledFor(logging.DEBUG):
                    _LOGGER.debug(
                        "eduVULCAN schedule changes count (pupil %s): %s",
                        pupil_id,
                        len(changes_envelope),
                    )
                    if changes_envelope:
                        _LOGGER.debug(
                            "eduVULCAN raw schedule changes (pupil %s): %s",
                            pupil_id,
                            json.dumps(changes_envelope, ensure_ascii=False, default=str),
                        )
                    # Full raw dump of the next 2 days of schedule entries —
                    # to find where this school hides substitution info.
                    horizon = {
                        (today + timedelta(days=offset)).isoformat()
                        for offset in (0, 1, 2)
                    }
                    for entry in envelope:
                        if str(entry.get("DateAt", ""))[:10] in horizon:
                            _LOGGER.debug(
                                "eduVULCAN raw lesson (pupil %s): %s",
                                pupil_id,
                                json.dumps(entry, ensure_ascii=False, default=str),
                            )
                if _LOGGER.isEnabledFor(logging.DEBUG):
                    if envelope:
                        _LOGGER.debug(
                            "eduVULCAN schedule keys (pupil %s): %s",
                            pupil_id,
                            sorted(envelope[0].keys()),
                        )
                    for entry in envelope:
                        if any(
                            entry.get(k)
                            for k in ("Substitution", "Change", "Event", "MergeChangeId")
                        ):
                            _LOGGER.debug(
                                "eduVULCAN raw changed lesson (pupil %s): %s",
                                pupil_id,
                                json.dumps(entry, ensure_ascii=False, default=str),
                            )
                    # Second suspect: the schedule/extra endpoint.
                    extra_envelope = await self.api._http.request(  # noqa: SLF001
                        method="GET",
                        rest_url=rest_url,
                        pupil_id=pupil_id,
                        endpoint="mobile/schedule/extra/withchanges/byPupil",
                        query={
                            "pupilId": pupil_id,
                            "dateFrom": today,
                            "dateTo": today + timedelta(days=SCHEDULE_DAYS_AHEAD),
                            "lastId": -2_147_483_648,
                            "pageSize": 500,
                            "lastSyncDate": datetime(1970, 1, 1, 1, 0, 0),
                        },
                    )
                    _LOGGER.debug(
                        "eduVULCAN raw schedule_extra (pupil %s): %s",
                        pupil_id,
                        json.dumps(extra_envelope, ensure_ascii=False, default=str),
                    )
                pupil_data.homework = await self.api.get_homework(
                    rest_url=rest_url,
                    pupil_id=pupil_id,
                    date_from=today - timedelta(days=7),
                    date_to=today + timedelta(days=HOMEWORK_DAYS_AHEAD),
                )
                pupil_data.exams = await self.api.get_exams(
                    rest_url=rest_url,
                    pupil_id=pupil_id,
                    date_from=today,
                    date_to=today + timedelta(days=EXAMS_DAYS_AHEAD),
                )
            except IrisApiException as err:
                raise UpdateFailed(
                    f"eduVULCAN: fetch failed for pupil {pupil_id}: {err}"
                ) from err

            # Lucky number is nice-to-have; don't fail the whole update on it.
            # NOTE: on weekends/holidays Vulcan returns an empty envelope (None),
            # which makes LuckyNumber.model_validate raise ValidationError —
            # catch broadly, this block must never kill the whole refresh.
            try:
                lucky = await self.api.get_lucky_number(
                    rest_url=rest_url,
                    pupil_id=pupil_id,
                    constituent_unit_id=account.constituent_unit.id,
                    day=today,
                )
                pupil_data.lucky_number = lucky.number
            except (IrisApiException, ValidationError, AttributeError, TypeError):
                _LOGGER.debug(
                    "Lucky number unavailable for pupil %s", pupil_id, exc_info=True
                )

            data[pupil_id] = pupil_data

        if not data:
            raise UpdateFailed("eduVULCAN: account list is empty")

        # Messages/announcements are nice-to-have; they must never kill the whole refresh.
        try:
            await self.messages.async_check([p.account for p in data.values()])
        except (IrisApiException, ValidationError, AttributeError, TypeError, KeyError):
            _LOGGER.warning("eduVULCAN messages update failed", exc_info=True)

        return data
