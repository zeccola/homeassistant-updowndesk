"""Diagnostics for the UpDown Desk integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant

from .coordinator import UpDownDeskConfigEntry

TO_REDACT = {CONF_ADDRESS, "unique_id", "title"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: UpDownDeskConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data
    state = coordinator.desk.state
    calibration = state.calibration

    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "state": {
            "connected": state.connected,
            "moving": state.moving,
            "direction": state.direction,
            "run_hall": state.run_hall,
            "target_hall": state.target_hall,
            "height_cm": state.height_cm,
            "position": state.position,
            "error_code": state.error_code,
            "error_text": state.error_text,
        },
        "calibration": None
        if calibration is None
        else {
            "base_hall": calibration.base_hall,
            "min_hall": calibration.min_hall,
            "max_hall": calibration.max_hall,
            "model": calibration.model,
            "counts_per_cm": calibration.counts_per_cm,
            "min_cm": calibration.min_cm,
            "max_cm": calibration.max_cm,
        },
        "desk": {
            "follow_handshake": coordinator.desk.follow_handshake,
            "idle_timeout": coordinator.desk.idle_timeout,
            "move_timeout": coordinator.desk.move_timeout,
        },
    }
