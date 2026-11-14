"""IQAir Cloud integration for Home Assistant.

Forked from ThioJoe/HA-IQAir-Integration (AGPL-3.0) and rewritten for the cloud API
IQAir has served since late October 2026. See protocol.py for how that API works.
"""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval

from .api import IQAirClient, create_client_session
from .const import CONF_LOGIN_TOKEN, CONF_SESSION, CONF_USER_ID, SESSION_CHECK_INTERVAL
from .coordinator import IQAirCoordinator
from .protocol import Login

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.FAN, Platform.NUMBER, Platform.SENSOR]

type IQAirConfigEntry = ConfigEntry[IQAirCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: IQAirConfigEntry) -> bool:
    """Set up one IQAir account."""
    data = entry.data
    login = None
    if data.get(CONF_USER_ID) and data.get(CONF_LOGIN_TOKEN):
        login = Login(user_id=data[CONF_USER_ID], login_token=data[CONF_LOGIN_TOKEN])

    @callback
    def store_login(new_login: Login) -> None:
        """Keep a refreshed login token so a restart does not need a new sign-in.

        There is deliberately no update listener on this entry: storing the token must
        not reload the integration.
        """
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_USER_ID: new_login.user_id, CONF_LOGIN_TOKEN: new_login.login_token},
        )

    client = IQAirClient(
        create_client_session(hass),
        email=data[CONF_EMAIL],
        password=data[CONF_PASSWORD],
        login=login,
        dashboard_session=data.get(CONF_SESSION),
        on_login=store_login,
    )
    coordinator = IQAirCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Find out about an expired dashboard session before an automation trips over it.
    entry.async_on_unload(
        async_track_time_interval(hass, coordinator.async_check_session, SESSION_CHECK_INTERVAL)
    )
    entry.async_create_task(hass, coordinator.async_check_session(), "iqair_cloud session check")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: IQAirConfigEntry) -> bool:
    """Unload an IQAir account."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Refuse entries made by the 1.x versions, which stored tokens that no longer exist."""
    if entry.version < 2:
        _LOGGER.error(
            "The IQAir Cloud entry '%s' was created by an older version that used an API "
            "IQAir has shut down. Delete it and add the integration again",
            entry.title,
        )
        return False
    return True
