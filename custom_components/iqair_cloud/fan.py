"""The purifier itself: power, fan speed and smart mode."""
from __future__ import annotations

import math
from typing import Any

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util.percentage import percentage_to_ranged_value, ranged_value_to_percentage

from . import protocol
from .coordinator import IQAirCoordinator
from .entity import IQAirEntity, shown_level
from .protocol import Device

# Home Assistant preset -> IQAir smart mode profile. The dashboard shows "power" as "Max".
PRESET_TO_PROFILE = {"quiet": "quiet", "balanced": "balanced", "max": "power"}
PROFILE_TO_PRESET = {profile: preset for preset, profile in PRESET_TO_PROFILE.items()}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Create a fan for every purifier on the account."""
    coordinator: IQAirCoordinator = entry.runtime_data
    async_add_entities(IQAirFan(coordinator, device_id) for device_id in coordinator.data)


class IQAirFan(IQAirEntity, FanEntity):
    """An IQAir purifier.

    Speeds are the purifier's own levels (6 on HealthPro XE and Atem Earth). A preset
    means smart mode with that profile; setting a speed returns to manual mode.
    """

    _attr_name = None
    _attr_translation_key = "purifier"

    def __init__(self, coordinator: IQAirCoordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id)
        self._attr_unique_id = device_id

    @property
    def available(self) -> bool:
        # IQAir cannot reach an offline purifier, so it can be neither read nor controlled.
        device = self.device
        return super().available and device is not None and device.connected

    @property
    def supported_features(self) -> FanEntityFeature:
        features = FanEntityFeature.SET_SPEED | FanEntityFeature.TURN_ON | FanEntityFeature.TURN_OFF
        device = self.device
        if device is not None and device.supports_smart_mode:
            features |= FanEntityFeature.PRESET_MODE
        return features

    @property
    def preset_modes(self) -> list[str] | None:
        device = self.device
        return list(PRESET_TO_PROFILE) if device is not None and device.supports_smart_mode else None

    @property
    def speed_count(self) -> int:
        device = self.device
        return (device.speed_count if device is not None else None) or 1

    @property
    def is_on(self) -> bool | None:
        device = self.device
        return device.is_on if device is not None else None

    @property
    def percentage(self) -> int | None:
        device = self.device
        if device is None or device.is_on is None:
            return None
        if not device.is_on:
            return 0
        level = shown_level(device)
        if not level or not device.speed_count:
            return None
        return ranged_value_to_percentage((1, device.speed_count), level)

    @property
    def preset_mode(self) -> str | None:
        device = self.device
        if device is None or not device.smart_mode:
            return None
        return PROFILE_TO_PRESET.get(device.smart_profile)

    async def async_turn_on(
        self, percentage: int | None = None, preset_mode: str | None = None, **kwargs: Any
    ) -> None:
        if preset_mode is not None:
            await self.async_set_preset_mode(preset_mode)
        elif percentage is not None:
            await self.async_set_percentage(percentage)
        else:
            await self._send(protocol.power_command(True))

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._send(protocol.power_command(False))

    async def async_set_percentage(self, percentage: int) -> None:
        if percentage == 0:
            await self.async_turn_off()
            return
        level = math.ceil(percentage_to_ranged_value((1, self.speed_count), percentage))
        await self.coordinator.async_set_fan_level(self._device_id, max(1, min(level, self.speed_count)))

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        device = self._current_device()
        profile = PRESET_TO_PROFILE[preset_mode]
        # Same order as the dashboard allows: on, then smart mode, then its profile.
        if not device.is_on:
            await self._send(protocol.power_command(True))
        if not device.smart_mode:
            await self._send(protocol.smart_mode_command(True))
        if device.smart_profile != profile:
            await self._send(protocol.smart_profile_command(profile))

    def _current_device(self) -> Device:
        device = self.device
        if device is None:
            raise HomeAssistantError("This purifier is no longer on the IQAir account")
        return device

    async def _send(self, command: dict[str, Any]) -> None:
        """Send one command. A failure raises, which stops any sequence it is part of."""
        await self.coordinator.async_send_command(self._device_id, command)
