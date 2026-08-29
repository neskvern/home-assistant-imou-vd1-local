"""Shared entity base for the Imou VD1 integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity import DeviceInfo, Entity

from .connection import CameraConnection
from .const import DOMAIN


class ImouVd1Entity(Entity):
    """Base entity providing shared device_info for all VD1 entities."""

    _attr_has_entity_name = True

    def __init__(self, conn: CameraConnection, entry: ConfigEntry) -> None:
        self._conn = conn
        self._entry = entry

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=self._entry.title,
            manufacturer="Imou",
            model="VD1",
        )
