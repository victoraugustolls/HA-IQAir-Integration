"""Fan speed as the purifier's own levels, for people who prefer 1-6 to percentages."""
from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import IQAirCoordinator
from .entity import IQAirEntity, shown_level


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Create a fan speed number for every purifier that reports its number of levels."""
    coordinator: IQAirCoordinator = entry.runtime_data
    async_add_entities(
        IQAirFanSpeed(coordinator, device.id) for device in coordinator.data.values() if device.speed_count
    )


class IQAirFanSpeed(IQAirEntity, NumberEntity):
    """The manual fan level, 1 to the purifier's highest level.

    Home Assistant's fan entity can only express speed as a percentage, which lands on
    values like 33% and 67% for six levels. This shows the level itself. Setting it
    returns the purifier to manual mode, exactly like setting a speed on the fan entity.
    """

    _attr_translation_key = "fan_speed"
    _attr_icon = "mdi:fan"
    _attr_mode = NumberMode.SLIDER
    _attr_native_min_value = 1
    _attr_native_step = 1

    def __init__(self, coordinator: IQAirCoordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id)
        self._attr_unique_id = f"{device_id}_fan_speed"

    @property
    def available(self) -> bool:
        device = self.device
        return super().available and device is not None and device.connected

    @property
    def native_max_value(self) -> float:
        device = self.device
        return (device.speed_count if device is not None else None) or 1

    @property
    def native_value(self) -> int | None:
        device = self.device
        return shown_level(device) if device is not None else None

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_fan_level(self._device_id, int(value))
