"""The Imou VD1 integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType

from .connection import CameraConnection
from .const import (
    CONF_CHANNEL,
    CONF_DVRIP_PORT,
    CONF_HTTP_PORT,
    CONF_IMOU_APP_ID,
    CONF_IMOU_APP_SECRET,
    CONF_IMOU_DATA_CENTER,
    CONF_IMOU_DEVICE_ID,
    CONF_STREAM,
    DEFAULT_IMOU_DATA_CENTER,
    DOMAIN,
    PLATFORMS,
)
from .stream import ImouVd1StreamView


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    hass.http.register_view(ImouVd1StreamView(hass))
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    data = entry.data

    conn = CameraConnection(
        host=data[CONF_HOST],
        username=data[CONF_USERNAME],
        password=data[CONF_PASSWORD],
        dvrip_port=data[CONF_DVRIP_PORT],
        http_port=data[CONF_HTTP_PORT],
        channel=data[CONF_CHANNEL],
        stream=data[CONF_STREAM],
        imou_app_id=data.get(CONF_IMOU_APP_ID),
        imou_app_secret=data.get(CONF_IMOU_APP_SECRET),
        imou_device_id=data.get(CONF_IMOU_DEVICE_ID),
        imou_data_center=data.get(CONF_IMOU_DATA_CENTER, DEFAULT_IMOU_DATA_CENTER),
    )

    await hass.async_add_executor_job(conn.wake)
    await hass.async_add_executor_job(conn.connect)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = conn

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        conn: CameraConnection = hass.data[DOMAIN].pop(entry.entry_id)
        await hass.async_add_executor_job(conn.close)
    return unload_ok
