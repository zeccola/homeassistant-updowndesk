"""Cover entity giving the desk up, down, stop and position control."""

from __future__ import annotations

from homeassistant.components.cover import (
    ATTR_POSITION,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_platform
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
import voluptuous as vol

from .const import (
    ATTR_DIRECTION,
    ATTR_DURATION,
    DEFAULT_JOG_SECONDS,
    MAX_JOG_SECONDS,
    SERVICE_JOG,
)
from .coordinator import UpDownDeskConfigEntry, UpDownDeskCoordinator
from .entity import UpDownDeskEntity

JOG_SCHEMA = {
    vol.Required(ATTR_DIRECTION): vol.In(["up", "down"]),
    vol.Optional(ATTR_DURATION, default=DEFAULT_JOG_SECONDS): vol.All(
        vol.Coerce(float), vol.Range(min=0.05, max=MAX_JOG_SECONDS)
    ),
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: UpDownDeskConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the desk cover and its jog service."""
    async_add_entities([UpDownDeskCover(entry.runtime_data)])

    entity_platform.async_get_current_platform().async_register_entity_service(
        SERVICE_JOG, JOG_SCHEMA, "async_jog"
    )


class UpDownDeskCover(UpDownDeskEntity, CoverEntity):
    """The desk itself: 0% is its lowest height, 100% its highest."""

    _attr_name = None
    _attr_translation_key = None
    _attr_icon = "mdi:desk"
    _attr_supported_features = (
        CoverEntityFeature.OPEN
        | CoverEntityFeature.CLOSE
        | CoverEntityFeature.STOP
        | CoverEntityFeature.SET_POSITION
    )

    def __init__(self, coordinator: UpDownDeskCoordinator) -> None:
        """Set up the cover."""
        super().__init__(coordinator, "desk")
        self._attr_name = None
        self._attr_translation_key = None

    @property
    def current_cover_position(self) -> int | None:
        """Height as a percentage of the desk's travel."""
        return self.desk_state.position

    @property
    def is_closed(self) -> bool | None:
        """True only at the very bottom of the range."""
        if (position := self.desk_state.position) is None:
            return None
        return position == 0

    @property
    def is_opening(self) -> bool:
        """True while rising."""
        state = self.desk_state
        return state.moving and state.direction == "up"

    @property
    def is_closing(self) -> bool:
        """True while lowering."""
        state = self.desk_state
        return state.moving and state.direction == "down"

    async def async_open_cover(self, **kwargs) -> None:
        """Raise the desk to its highest position."""
        await self.coordinator.async_move_to_position(100)

    async def async_close_cover(self, **kwargs) -> None:
        """Lower the desk to its lowest position."""
        await self.coordinator.async_move_to_position(0)

    async def async_set_cover_position(self, **kwargs) -> None:
        """Move the desk to a position between 0 and 100."""
        await self.coordinator.async_move_to_position(kwargs[ATTR_POSITION])

    async def async_stop_cover(self, **kwargs) -> None:
        """Stop the desk where it is."""
        await self.coordinator.async_stop()

    async def async_jog(self, direction: str, duration: float) -> None:
        """Nudge the desk for a short time (the ``jog`` service)."""
        await self.coordinator.async_jog(direction, duration)
