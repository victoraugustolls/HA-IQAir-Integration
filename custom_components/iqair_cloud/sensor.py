"""Air readings and filter life for IQAir purifiers."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfDensity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import IQAirCoordinator
from .entity import IQAirEntity
from .protocol import Device, Filter


@dataclass(frozen=True, kw_only=True)
class IQAirReadingDescription(SensorEntityDescription):
    """A reading taken by the purifier's own sensor."""

    value_fn: Callable[[Device], float | None]


READINGS: tuple[IQAirReadingDescription, ...] = (
    IQAirReadingDescription(
        key="pm25",
        device_class=SensorDeviceClass.PM25,
        native_unit_of_measurement=UnitOfDensity.MICROGRAMS_PER_CUBIC_METER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda device: device.pm25,
    ),
    IQAirReadingDescription(
        # No unit: IQAir's API labels it "ppb" and its dashboard "/L", so neither is trusted.
        key="particle_count",
        translation_key="particle_count",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda device: device.particle_count,
    ),
    IQAirReadingDescription(
        key="aqi",
        device_class=SensorDeviceClass.AQI,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda device: device.aqi,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Create the sensors for every purifier on the account."""
    coordinator: IQAirCoordinator = entry.runtime_data
    entities: list[SensorEntity] = []
    for device in coordinator.data.values():
        entities.extend(IQAirReadingSensor(coordinator, device.id, description) for description in READINGS)
        entities.extend(IQAirFilterSensor(coordinator, device.id, filter_) for filter_ in device.filters)
    async_add_entities(entities)


class IQAirReadingSensor(IQAirEntity, SensorEntity):
    """PM2.5, particle count or AQI as measured by the purifier."""

    entity_description: IQAirReadingDescription

    def __init__(
        self, coordinator: IQAirCoordinator, device_id: str, description: IQAirReadingDescription
    ) -> None:
        super().__init__(coordinator, device_id)
        self.entity_description = description
        self._attr_unique_id = f"{device_id}_{description.key}"

    @property
    def available(self) -> bool:
        # An offline purifier's last reading says nothing about the air now.
        device = self.device
        return super().available and device is not None and device.connected

    @property
    def native_value(self) -> float | None:
        device = self.device
        return self.entity_description.value_fn(device) if device is not None else None


class IQAirFilterSensor(IQAirEntity, SensorEntity):
    """Remaining life of one filter, in percent."""

    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:air-filter"

    def __init__(self, coordinator: IQAirCoordinator, device_id: str, filter_: Filter) -> None:
        super().__init__(coordinator, device_id)
        self._slot = filter_.slot
        self._attr_unique_id = f"{device_id}_filter_{filter_.slot}_life"
        self._attr_name = f"{filter_.name} life"

    @property
    def native_value(self) -> int | None:
        device = self.device
        if device is None:
            return None
        return next((f.health_percent for f in device.filters if f.slot == self._slot), None)
