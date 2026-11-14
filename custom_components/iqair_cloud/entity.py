"""Base entity for IQAir purifiers."""
from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import IQAirCoordinator
from .protocol import Device


def shown_level(device: Device) -> int | None:
    """The fan level to display.

    In smart mode the purifier picks the speed, so show what it is running at;
    otherwise show the manual setting.
    """
    level = (device.running_speed if device.smart_mode else device.speed_level) or (
        device.speed_level or device.running_speed
    )
    if not level:
        return None
    return min(level, device.speed_count) if device.speed_count else level


class IQAirEntity(CoordinatorEntity[IQAirCoordinator]):
    """An entity that belongs to one purifier."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: IQAirCoordinator, device_id: str) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        device = coordinator.data[device_id]
        # The serial number is left out on purpose: IQAir's old command API treats it
        # as the only thing identifying a purifier.
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device_id)},
            name=device.name,
            manufacturer="IQAir",
            model=device.model_label,
            model_id=device.model_variation,
        )

    @property
    def device(self) -> Device | None:
        """The purifier's latest state, or None if it is gone from the account."""
        return self.coordinator.data.get(self._device_id)

    @property
    def available(self) -> bool:
        return super().available and self.device is not None
