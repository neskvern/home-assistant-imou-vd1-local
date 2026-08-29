"""Motion binary sensor for the Imou VD1 integration."""

from __future__ import annotations

import json
import logging

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import ImouVd1Entity

_LOGGER = logging.getLogger(__name__)

# Observed DVRIP event Codes for motion-adjacent detections; the camera
# picks whichever is most specific (e.g. SmartMotionHuman for a person)
# rather than always sending plain VideoMotion, so all known variants
# are treated as "motion" here.
_MOTION_CODES = {"VideoMotion", "SmartMotionHuman", "SmartMotionVehicle"}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    conn = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([ImouVd1MotionSensor(conn, entry)])


class ImouVd1MotionSensor(ImouVd1Entity, BinarySensorEntity):
    """Motion sensor driven by DVRIP VideoMotion events.

    subscribe_events()'s callback runs on the DVRIP reader thread, not
    the event loop, so state updates must go through the synchronous
    schedule_update_ha_state() - not the async_ variant, and not a
    manual run_coroutine_threadsafe - since that's exactly the seam HA
    provides for updating entity state from a foreign thread.
    """

    _attr_translation_key = "motion"
    _attr_device_class = BinarySensorDeviceClass.MOTION

    def __init__(self, conn, entry) -> None:
        super().__init__(conn, entry)
        self._attr_unique_id = f"{entry.entry_id}-motion"
        self._attr_is_on = False
        self._token: int | None = None

    async def async_added_to_hass(self) -> None:
        # codes=None: the camera's own eventManager.attach already asks
        # for every code ("All"); which of those are motion is decided
        # below by parsing each event's actual "Code" field, not by
        # CameraConnection's substring-based subscriber filter (that
        # filter checks for the literal text of `codes` inside the raw
        # body, so it can't express "match any of several codes").
        self._token = self._conn.subscribe_events(self._handle_event, codes=None)
        _LOGGER.debug("Subscribed to events")

    async def async_will_remove_from_hass(self) -> None:
        if self._token is not None:
            self._conn.unsubscribe_events(self._token)
            self._token = None

    def _handle_event(self, body: bytes) -> None:
        _LOGGER.debug("Event: %s", body)

        try:
            payload = json.loads(body)
        except ValueError:
            return

        for event in payload.get("params", {}).get("eventList", []):
            if event.get("Code") not in _MOTION_CODES:
                continue

            is_on = event.get("Action") != "Stop"
            if is_on == self._attr_is_on:
                return

            self._attr_is_on = is_on
            self.schedule_update_ha_state()
            return
