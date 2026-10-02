"""Sensors for the UpDown Desk integration."""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfLength
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
    """Set up the desk's sensors."""
    coordinator = entry.runtime_data
    async_add_entities(
        [
            UpDownDeskHeightSensor(coordinator),
            UpDownDeskHallSensor(coordinator),
            UpDownDeskErrorSensor(coordinator),
        ]
    )


class UpDownDeskHeightSensor(UpDownDeskEntity, SensorEntity):
    """The desk's current height."""

    _attr_device_class = SensorDeviceClass.DISTANCE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfLength.CENTIMETERS
    _attr_suggested_display_precision = 1

    def __init__(self, coordinator: UpDownDeskCoordinator) -> None:
        """Set up the height sensor."""
        super().__init__(coordinator, "height")

    @property
    def native_value(self) -> float | None:
        """Height in centimetres, truncated the way the handset shows it."""
        if (height := self.desk_state.height_cm) is None:
            return None
        return truncate_cm(height)


class UpDownDeskHallSensor(UpDownDeskEntity, SensorEntity):
    """Raw hall counter, for diagnosing the height conversion."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: UpDownDeskCoordinator) -> None:
        """Set up the hall counter sensor."""
        super().__init__(coordinator, "run_hall")

    @property
    def native_value(self) -> int | None:
        """The position the desk reports, in hall counts."""
        return self.desk_state.run_hall

    @property
    def extra_state_attributes(self) -> dict[str, int | float] | None:
        """Expose the calibration this desk reported."""
        if (calibration := self.desk_state.calibration) is None:
            return None
        return {
            "base_hall": calibration.base_hall,
            "min_hall": calibration.min_hall,
            "max_hall": calibration.max_hall,
            "model": calibration.model,
            "counts_per_cm": calibration.counts_per_cm,
        }


class UpDownDeskErrorSensor(UpDownDeskEntity, SensorEntity):
    """The most recent error the controller reported."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: UpDownDeskCoordinator) -> None:
        """Set up the error sensor."""
        super().__init__(coordinator, "error")

    @property
    def native_value(self) -> str:
        """The error text, or ``none``."""
        return self.desk_state.error_text or "none"
