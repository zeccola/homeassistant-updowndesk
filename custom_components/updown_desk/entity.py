"""Shared entity base for the UpDown Desk integration."""

from __future__ import annotations

from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, MODEL
from .coordinator import UpDownDeskCoordinator
from .desk import DeskState


class UpDownDeskEntity(CoordinatorEntity[UpDownDeskCoordinator]):
    """Base entity tying everything to the one desk device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: UpDownDeskCoordinator, key: str) -> None:
        """Set up the entity."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.address)},
            connections={(CONNECTION_BLUETOOTH, coordinator.address)},
            name=coordinator.config_entry.title,
            manufacturer=MANUFACTURER,
            model=MODEL,
        )

    @property
    def desk_state(self) -> DeskState:
        """The desk's current state."""
        return self.coordinator.desk.state

    @property
    def available(self) -> bool:
        """Whether the desk is reachable."""
        if self.coordinator.desk.idle_timeout is not None:
            # On-demand mode: being disconnected between commands is normal.
            return super().available
        return super().available and self.desk_state.connected
