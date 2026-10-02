"""Binary sensors for the UpDown Desk integration."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import UpDownDeskConfigEntry, UpDownDeskCoordinator
from .entity import UpDownDeskEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: UpDownDeskConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the desk's binary sensors."""
    coordinator = entry.runtime_data
    async_add_entities(
        [
            UpDownDeskMovingSensor(coordinator),
            UpDownDeskProblemSensor(coordinator),
            UpDownDeskConnectionSensor(coordinator),
        ]
    )


class UpDownDeskMovingSensor(UpDownDeskEntity, BinarySensorEntity):
    """Whether the desk is travelling."""

    _attr_device_class = BinarySensorDeviceClass.MOVING

    def __init__(self, coordinator: UpDownDeskCoordinator) -> None:
        """Set up the moving sensor."""
        super().__init__(coordinator, "moving")

    @property
    def is_on(self) -> bool:
        """True while the motors are running."""
        return self.desk_state.moving

    @property
    def extra_state_attributes(self) -> dict[str, str | float | None]:
        """Where it is heading, while it is heading there."""
        state = self.desk_state
        return {"direction": state.direction, "target_height": state.target_cm}


class UpDownDeskProblemSensor(UpDownDeskEntity, BinarySensorEntity):
    """Whether the controller is reporting a fault."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: UpDownDeskCoordinator) -> None:
        """Set up the problem sensor."""
        super().__init__(coordinator, "problem")

    @property
    def is_on(self) -> bool:
        """True while an error is outstanding."""
        return self.desk_state.error_code is not None

    @property
    def extra_state_attributes(self) -> dict[str, str | int | None]:
        """The raw and decoded error."""
        state = self.desk_state
        return {"error_code": state.error_code, "error": state.error_text}


class UpDownDeskConnectionSensor(UpDownDeskEntity, BinarySensorEntity):
    """Whether Home Assistant currently holds the desk's single BLE slot."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: UpDownDeskCoordinator) -> None:
        """Set up the connection sensor."""
        super().__init__(coordinator, "connected")

    @property
    def available(self) -> bool:
        """Always available: reporting "disconnected" is the point."""
        return True

    @property
    def is_on(self) -> bool:
        """True while connected."""
        return self.desk_state.connected
