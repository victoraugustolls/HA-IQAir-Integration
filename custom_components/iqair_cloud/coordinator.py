"""Polls IQAir for every purifier on the account and sends commands to them."""
from __future__ import annotations

from datetime import datetime
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from . import protocol
from .api import IQAirClient
from .const import DOMAIN, MAX_SCAN_INTERVAL, SCAN_INTERVAL
from .protocol import Device

_LOGGER = logging.getLogger(__name__)


class IQAirCoordinator(DataUpdateCoordinator[dict[str, Device]]):
    """Holds the latest state of each purifier, keyed by device id."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: IQAirClient) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name=DOMAIN, update_interval=SCAN_INTERVAL)
        self.client = client
        self._failed_polls = 0

    async def _async_update_data(self) -> dict[str, Device]:
        try:
            devices = await self.client.async_get_devices()
        except protocol.InvalidCredentials as err:
            raise ConfigEntryAuthFailed("IQAir no longer accepts the stored email and password") from err
        except protocol.IQAirError as err:
            # Back off: double the wait after each failed poll, up to the cap.
            self._failed_polls += 1
            self.update_interval = min(SCAN_INTERVAL * 2 ** min(self._failed_polls, 10), MAX_SCAN_INTERVAL)
            raise UpdateFailed(str(err)) from err
        self._failed_polls = 0
        self.update_interval = SCAN_INTERVAL
        return {device.id: device for device in devices}

    async def async_check_session(self, _now: datetime | None = None) -> None:
        """Ask for a new session cookie if the dashboard has stopped accepting this one."""
        if not self.client.has_dashboard_session:
            return
        try:
            accepted = await self.client.async_check_session()
        except protocol.IQAirError as err:
            # Not a clear "no": do not bother the user over an outage.
            _LOGGER.debug("Could not check the IQAir dashboard session: %s", err)
            return
        if not accepted:
            self._session_rejected()

    def _session_rejected(self) -> None:
        _LOGGER.warning(
            "IQAir no longer accepts the dashboard session. Readings continue, but the "
            "purifiers cannot be controlled until a new session cookie is entered"
        )
        self.config_entry.async_start_reauth(self.hass)

    async def async_set_fan_level(self, device_id: str, level: int) -> None:
        """Set a manual fan level. The dashboard only allows it while the purifier is on."""
        device = self.data.get(device_id)
        if device is None:
            raise HomeAssistantError("This purifier is no longer on the IQAir account")
        if not device.is_on:
            await self.async_send_command(device_id, protocol.power_command(True))
        await self.async_send_command(device_id, protocol.fan_speed_command(level))

    async def async_send_command(self, device_id: str, command: dict[str, Any]) -> None:
        """Send one command and show its effect right away. Raises HomeAssistantError."""
        if not self.client.has_dashboard_session:
            raise HomeAssistantError(
                "Controlling IQAir purifiers needs a dashboard session cookie. "
                "Add one with Reconfigure on the IQAir Cloud integration",
                translation_domain=DOMAIN,
                translation_key="control_not_configured",
            )
        try:
            await self.client.async_send_command(device_id, command)
        except protocol.SessionExpired as err:
            self._session_rejected()
            raise HomeAssistantError(
                "IQAir no longer accepts the dashboard session. Enter a new session cookie to control the purifier",
                translation_domain=DOMAIN,
                translation_key="session_expired",
            ) from err
        except protocol.CommandRejected as err:
            raise HomeAssistantError(
                f"IQAir refused the command: {err}",
                translation_domain=DOMAIN,
                translation_key="command_rejected",
                translation_placeholders={"reason": str(err)},
            ) from err
        except protocol.IQAirError as err:
            raise HomeAssistantError(
                f"The command did not reach IQAir: {err}",
                translation_domain=DOMAIN,
                translation_key="command_failed",
                translation_placeholders={"reason": str(err)},
            ) from err

        device = self.data.get(device_id)
        if device is not None:
            # The next poll confirms it. This also restarts the poll timer.
            self.async_set_updated_data({**self.data, device_id: protocol.apply_command(device, command)})
