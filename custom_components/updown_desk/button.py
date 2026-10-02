"""Buttons for the UpDown Desk integration."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DEFAULT_JOG_SECONDS
from .coordinator import UpDownDeskConfigEntry, UpDownDeskCoordinator
from .entity import UpDownDeskEntity
from .protocol import Op


@dataclass(frozen=True, kw_only=True)
class UpDownDeskButtonDescription(ButtonEntityDescription):
    """Describes a desk button."""

    action: Callable[[UpDownDeskCoordinator], Coroutine[Any, Any, None]]


BUTTONS: tuple[UpDownDeskButtonDescription, ...] = (
    UpDownDeskButtonDescription(
        key="sit",
        translation_key="sit",
        icon="mdi:seat-outline",
        action=lambda c: c.async_recall_preset(Op.PRESET_SIT),
    ),
    UpDownDeskButtonDescription(
        key="middle",
        translation_key="middle",
        icon="mdi:format-align-middle",
        action=lambda c: c.async_recall_preset(Op.PRESET_MIDDLE),
    ),
    UpDownDeskButtonDescription(
        key="stand",
        translation_key="stand",
        icon="mdi:human-handsup",
        action=lambda c: c.async_recall_preset(Op.PRESET_STAND),
    ),
    UpDownDeskButtonDescription(
        key="stop",
        translation_key="stop",
        icon="mdi:stop",
        action=lambda c: c.async_stop(),
    ),
    UpDownDeskButtonDescription(
        key="nudge_up",
        translation_key="nudge_up",
        icon="mdi:arrow-up",
        action=lambda c: c.async_jog("up", DEFAULT_JOG_SECONDS),
    ),
    UpDownDeskButtonDescription(
        key="nudge_down",
        translation_key="nudge_down",
        icon="mdi:arrow-down",
        action=lambda c: c.async_jog("down", DEFAULT_JOG_SECONDS),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: UpDownDeskConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the desk's buttons."""
    coordinator = entry.runtime_data
    async_add_entities(
        UpDownDeskButton(coordinator, description) for description in BUTTONS
    )


class UpDownDeskButton(UpDownDeskEntity, ButtonEntity):
    """A one-shot desk command."""

    entity_description: UpDownDeskButtonDescription

    def __init__(
        self,
        coordinator: UpDownDeskCoordinator,
        description: UpDownDeskButtonDescription,
    ) -> None:
        """Set up the button."""
        super().__init__(coordinator, description.key)
        self.entity_description = description

    async def async_press(self) -> None:
        """Run the button's command."""
        await self.entity_description.action(self.coordinator)
