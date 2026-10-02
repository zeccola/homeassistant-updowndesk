"""Target height number entity for the UpDown Desk integration."""

from __future__ import annotations

import math

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberMode,
)
from homeassistant.const import UnitOfLength
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import UpDownDeskConfigEntry, UpDownDeskCoordinator
from .entity import UpDownDeskEntity
from .protocol import truncate_cm


async def async_setup_entry(
    hass: HomeAssistant,
    entry: UpDownDeskConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the target height number."""
    async_add_entities([UpDownDeskHeightNumber(entry.runtime_data)])


class UpDownDeskHeightNumber(UpDownDeskEntity, NumberEntity):
    """Move the desk to an exact height."""

    _attr_device_class = NumberDeviceClass.DISTANCE
    _attr_native_unit_of_measurement = UnitOfLength.CENTIMETERS
    _attr_native_step = 0.5
    _attr_mode = NumberMode.SLIDER
    _attr_icon = "mdi:arrow-up-down"

    def __init__(self, coordinator: UpDownDeskCoordinator) -> None:
        """Set up the number, bounding it by the desk's own travel limits."""
        super().__init__(coordinator, "target_height")
        calibration = coordinator.desk.require_calibration()
        # Pull the bounds inwards to the next step the desk can actually reach.
        self._attr_native_min_value = math.ceil(calibration.min_cm * 2) / 2
        self._attr_native_max_value = math.floor(calibration.max_cm * 2) / 2

    @property
    def native_value(self) -> float | None:
        """The height being moved to, or the current height when idle."""
        state = self.desk_state
        if (target := state.target_cm) is not None:
            return truncate_cm(target)
        if (height := state.height_cm) is None:
            return None
        return truncate_cm(height)

    async def async_set_native_value(self, value: float) -> None:
        """Move the desk to ``value`` centimetres."""
        await self.coordinator.async_move_to_cm(value)
