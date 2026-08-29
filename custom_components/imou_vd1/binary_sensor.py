"""Motion binary sensor for the Imou VD1 integration."""

from __future__ import annotations

import re

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import ImouVd1Entity

_ACTION_RE = re.compile(rb'"Action"\s*:\s*"(\w+)"')


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
        self._token = self._conn.subscribe_events(self._handle_event, codes=["VideoMotion"])

    async def async_will_remove_from_hass(self) -> None:
        if self._token is not None:
            self._conn.unsubscribe_events(self._token)
            self._token = None

    def _handle_event(self, body: bytes) -> None:
        match = _ACTION_RE.search(body)
        is_on = match.group(1) != b"Stop" if match else True

        if is_on == self._attr_is_on:
            return

        self._attr_is_on = is_on
        self.schedule_update_ha_state()
