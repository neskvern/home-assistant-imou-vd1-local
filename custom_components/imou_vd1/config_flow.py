"""Config flow for the Imou VD1 integration."""

from __future__ import annotations

import time
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.data_entry_flow import FlowResult

from .connection import (
    IMOU_DATA_CENTERS,
    WAKE_SETTLE_DELAY,
    open_dvrip_connection,
    wake_camera,
)
from .const import (
    CONF_CHANNEL,
    CONF_DVRIP_PORT,
    CONF_HTTP_PORT,
    CONF_IMOU_APP_ID,
    CONF_IMOU_APP_SECRET,
    CONF_IMOU_DATA_CENTER,
    CONF_IMOU_DEVICE_ID,
    CONF_STREAM,
    DEFAULT_CHANNEL,
    DEFAULT_DVRIP_PORT,
    DEFAULT_HTTP_PORT,
    DEFAULT_IMOU_DATA_CENTER,
    DEFAULT_STREAM,
    DOMAIN,
)

DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_USERNAME, default="admin"): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Required(CONF_CHANNEL, default=DEFAULT_CHANNEL): int,
        vol.Required(CONF_STREAM, default=DEFAULT_STREAM): int,
        vol.Optional(CONF_IMOU_APP_ID): str,
        vol.Optional(CONF_IMOU_APP_SECRET): str,
        vol.Optional(CONF_IMOU_DEVICE_ID): str,
        vol.Optional(CONF_IMOU_DATA_CENTER, default=DEFAULT_IMOU_DATA_CENTER): vol.In(
            sorted(IMOU_DATA_CENTERS)
        ),
    }
)


def _test_connection(user_input: dict[str, Any]) -> None:
    """Quick synchronous connectivity probe: dial + login, then close.

    Tries a bare connect first; if that fails, wakes the camera via the
    Imou Cloud API (a no-op if no cloud credentials were entered) and
    retries once - same wake-and-retry-once pattern as
    connect_dvrip_or_wake in connection.py. If no cloud credentials were
    given, the retry fails the same way the bare attempt did.
    """
    try:
        sock, _session = open_dvrip_connection(
            user_input[CONF_HOST],
            user_input[CONF_DVRIP_PORT],
            user_input[CONF_USERNAME],
            user_input[CONF_PASSWORD],
        )
    except Exception:
        wake_camera(
            user_input.get(CONF_IMOU_APP_ID),
            user_input.get(CONF_IMOU_APP_SECRET),
            user_input.get(CONF_IMOU_DEVICE_ID),
            user_input[CONF_IMOU_DATA_CENTER],
        )
        time.sleep(WAKE_SETTLE_DELAY)
        sock, _session = open_dvrip_connection(
            user_input[CONF_HOST],
            user_input[CONF_DVRIP_PORT],
            user_input[CONF_USERNAME],
            user_input[CONF_PASSWORD],
        )
    sock.close()


class ImouVd1ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Imou VD1."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            user_input[CONF_DVRIP_PORT] = DEFAULT_DVRIP_PORT
            user_input[CONF_HTTP_PORT] = DEFAULT_HTTP_PORT

            await self.async_set_unique_id(f"{user_input[CONF_HOST]}:{user_input[CONF_CHANNEL]}")
            self._abort_if_unique_id_configured()

            try:
                await self.hass.async_add_executor_job(_test_connection, user_input)
            except Exception:
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(title=user_input[CONF_HOST], data=user_input)

        return self.async_show_form(step_id="user", data_schema=DATA_SCHEMA, errors=errors)
