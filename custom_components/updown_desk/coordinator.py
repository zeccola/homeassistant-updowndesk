"""Coordinator holding the desk connection for a config entry."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
import contextlib
from datetime import time as dt_time, timedelta
import logging
from typing import Any

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_BASE_HALL,
    CONF_FOLLOW_HANDSHAKE,
    CONF_KEEP_CONNECTED,
    CONF_MAX_HALL,
    CONF_MIN_HALL,
    CONF_MODEL,
    CONF_MOVE_TIMEOUT,
    CONF_POLL_INTERVAL,
    CONF_QUIET_END,
    CONF_QUIET_START,
    DEFAULT_FOLLOW_HANDSHAKE,
    DEFAULT_KEEP_CONNECTED,
    DEFAULT_MOVE_TIMEOUT,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    IDLE_DISCONNECT_SECONDS,
)
from .desk import DeskError, DeskState, UpDownDesk
from .protocol import Calibration

_LOGGER = logging.getLogger(__name__)

type UpDownDeskConfigEntry = ConfigEntry[UpDownDeskCoordinator]


class UpDownDeskCoordinator(DataUpdateCoordinator[DeskState]):
    """Keeps one desk's state fresh and owns its movement task."""

    config_entry: UpDownDeskConfigEntry

    def __init__(self, hass: HomeAssistant, entry: UpDownDeskConfigEntry) -> None:
        """Set up the coordinator from a config entry."""
        self.address: str = entry.data[CONF_ADDRESS]
        options = entry.options
        poll_interval = options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)

        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {self.address}",
            update_interval=timedelta(seconds=poll_interval) if poll_interval else None,
        )

        self.desk = UpDownDesk(
            entry.title,
            self.address,
            self._async_ble_device,
            calibration=_calibration_from_entry(entry),
        )
        self.desk.move_timeout = options.get(CONF_MOVE_TIMEOUT, DEFAULT_MOVE_TIMEOUT)
        self.desk.idle_timeout = (
            None
            if options.get(CONF_KEEP_CONNECTED, DEFAULT_KEEP_CONNECTED)
            else IDLE_DISCONNECT_SECONDS
        )

        self._quiet_start = _parse_time(options.get(CONF_QUIET_START))
        self._quiet_end = _parse_time(options.get(CONF_QUIET_END))
        self._follow_handshake = options.get(
            CONF_FOLLOW_HANDSHAKE, DEFAULT_FOLLOW_HANDSHAKE
        )
        self._move_task: asyncio.Task | None = None
        self._unsub_desk: Callable[[], None] | None = None

    # ----------------------------------------------------------------- set up

    async def async_initialise(self) -> None:
        """Connect and make sure calibration is known before entities load."""
        self._unsub_desk = self.desk.add_listener(self._handle_desk_update)
        self._apply_quiet_state()
        try:
            await self.desk.async_connect()
            if self.desk.calibration is None:
                calibration = await self.desk.async_read_calibration()
                self._store_calibration(calibration)
        except DeskError:
            await self.desk.async_disconnect()
            raise

    async def async_shutdown_desk(self) -> None:
        """Stop any movement and release the desk."""
        await self.async_stop()
        if self._unsub_desk is not None:
            self._unsub_desk()
            self._unsub_desk = None
        await self.desk.async_disconnect()

    @callback
    def _async_ble_device(self):
        """Look the desk up fresh, so adapters and proxies are followed."""
        return bluetooth.async_ble_device_from_address(
            self.hass, self.address, connectable=True
        )

    @callback
    def _handle_desk_update(self) -> None:
        """Push desk state straight to the entities."""
        self.async_set_updated_data(self.desk.state)

    def _store_calibration(self, calibration: Calibration) -> None:
        """Cache calibration in the entry so restarts skip the INIT walk."""
        data = {
            **self.config_entry.data,
            CONF_BASE_HALL: calibration.base_hall,
            CONF_MIN_HALL: calibration.min_hall,
            CONF_MAX_HALL: calibration.max_hall,
            CONF_MODEL: calibration.model,
        }
        if data != dict(self.config_entry.data):
            self.hass.config_entries.async_update_entry(self.config_entry, data=data)

    # ---------------------------------------------------------------- polling

    async def _async_update_data(self) -> DeskState:
        """Poll the height, unless something better is already happening."""
        self._apply_quiet_state()

        if self.desk.state.moving:
            # The move loop samples far faster than we would.
            return self.desk.state

        if self._in_quiet_hours():
            # Polling wakes the desk's LED display, so leave it alone.
            _LOGGER.debug("%s: in quiet hours, skipping poll", self.name)
            return self.desk.state

        try:
            await self.desk.async_refresh()
        except DeskError as err:
            raise UpdateFailed(str(err)) from err
        return self.desk.state

    def _apply_quiet_state(self) -> None:
        quiet = self._in_quiet_hours()
        self.desk.follow_handshake = self._follow_handshake and not quiet

    def _in_quiet_hours(self) -> bool:
        """Whether background chatter should be suppressed right now."""
        return self._in_quiet_hours_at(dt_util.now().time())

    def _in_quiet_hours_at(self, now: dt_time) -> bool:
        """Whether ``now`` falls inside the quiet window.

        Both bounds are needed for a window; the end is exclusive.
        """
        if self._quiet_start is None or self._quiet_end is None:
            return False
        if self._quiet_start <= self._quiet_end:
            return self._quiet_start <= now < self._quiet_end
        # The window wraps midnight.
        return now >= self._quiet_start or now < self._quiet_end

    # --------------------------------------------------------------- movement

    async def async_move_to_cm(self, height_cm: float) -> None:
        """Start a closed loop move to a height in centimetres."""
        await self._start_move(lambda: self.desk.async_move_to_cm(height_cm))

    async def async_move_to_position(self, position: int) -> None:
        """Start a closed loop move to a 0-100 position."""
        await self._start_move(lambda: self.desk.async_move_to_position(position))

    async def async_recall_preset(self, op) -> None:
        """Recall a stored preset."""
        await self._start_move(lambda: self.desk.async_recall_preset(op))

    async def async_jog(self, direction: str, duration: float) -> None:
        """Nudge the desk for a fixed time."""
        await self._start_move(lambda: self.desk.async_jog(direction, duration))

    async def async_stop(self) -> None:
        """Cancel any movement and stop the motors."""
        await self._cancel_move()
        with contextlib.suppress(DeskError):
            await self.desk.async_stop()
        self._handle_desk_update()

    async def _start_move(self, factory: Callable[[], Coroutine[Any, Any, None]]) -> None:
        """Run a movement in the background so the service call returns."""
        await self._cancel_move()
        self._move_task = self.config_entry.async_create_background_task(
            self.hass, self._run_move(factory()), f"{DOMAIN} move {self.address}"
        )

    async def _run_move(self, coro: Coroutine[Any, Any, None]) -> None:
        try:
            await coro
        except DeskError as err:
            _LOGGER.error("%s: move failed: %s", self.name, err)
        finally:
            self._move_task = None
            self._handle_desk_update()

    async def _cancel_move(self) -> None:
        """Cancel a running move and wait for its STOP to land."""
        task, self._move_task = self._move_task, None
        if task is None or task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def _calibration_from_entry(entry: ConfigEntry) -> Calibration | None:
    """Rebuild cached calibration from the config entry, if present."""
    keys = (CONF_BASE_HALL, CONF_MIN_HALL, CONF_MAX_HALL, CONF_MODEL)
    if not all(key in entry.data for key in keys):
        return None
    return Calibration(
        base_hall=entry.data[CONF_BASE_HALL],
        min_hall=entry.data[CONF_MIN_HALL],
        max_hall=entry.data[CONF_MAX_HALL],
        model=entry.data[CONF_MODEL],
    )


def _parse_time(value: str | None) -> dt_time | None:
    """Parse an optional ``HH:MM[:SS]`` option value."""
    if not value:
        return None
    return dt_util.parse_time(value)
