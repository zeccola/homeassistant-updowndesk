"""The UpDown Desk integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .coordinator import UpDownDeskConfigEntry, UpDownDeskCoordinator
from .desk import DeskCalibrationError, DeskError

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.COVER,
    Platform.NUMBER,
    Platform.SENSOR,
]


async def async_setup_entry(hass: HomeAssistant, entry: UpDownDeskConfigEntry) -> bool:
    """Set up a desk from a config entry."""
    coordinator = UpDownDeskCoordinator(hass, entry)

    try:
        await coordinator.async_initialise()
    except DeskCalibrationError as err:
        # Retrying will not help until the desk reports usable geometry.
        raise ConfigEntryNotReady(
            f"Desk {entry.data[CONF_ADDRESS]} did not report usable calibration: {err}"
        ) from err
    except DeskError as err:
        raise ConfigEntryNotReady(str(err)) from err

    entry.runtime_data = coordinator
    await coordinator.async_config_entry_first_refresh()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: UpDownDeskConfigEntry) -> bool:
    """Stop the desk and tear the entry down."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    await entry.runtime_data.async_shutdown_desk()
    return unloaded


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload when the options change."""
    await hass.config_entries.async_reload(entry.entry_id)
