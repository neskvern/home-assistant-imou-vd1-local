"""Wake button for the Imou VD1 integration."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import ImouVd1Entity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    conn = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([ImouVd1WakeButton(conn, entry)])


class ImouVd1WakeButton(ImouVd1Entity, ButtonEntity):
    """Button that sends the Imou Cloud wake-up call to the camera."""

    _attr_translation_key = "wake"
    _attr_unique_id_suffix = "wake"

    def __init__(self, conn, entry) -> None:
        super().__init__(conn, entry)
        self._attr_unique_id = f"{entry.entry_id}-wake"

    async def async_press(self) -> None:
        await self.hass.async_add_executor_job(self._conn.wake)
