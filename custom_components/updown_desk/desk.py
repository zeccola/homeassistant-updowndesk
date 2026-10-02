"""Bluetooth transport and command layer for a PairLink standing desk.

The firmware has no "go to height X" command, so arbitrary heights are reached
by holding UP or DOWN while sampling the height, and stopping short by the
distance the desk is expected to coast.  Every path that starts the motors ends
in a STOP, including timeouts, faults and cancellation.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
import contextlib
from dataclasses import dataclass
import logging

from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    establish_connection,
)

from .const import (
    DEADBAND_HALL,
    DEFAULT_MOVE_TIMEOUT,
    MAX_JOG_SECONDS,
    MOVE_MAX_PASSES,
    MOVE_POLL_INTERVAL,
    PRESET_SETTLE_SECONDS,
    REQUEST_RETRIES,
    REQUEST_TIMEOUT,
    RETRY_THRESHOLD_HALL,
    STALL_TIMEOUT,
    STOP_LEAD_SECONDS,
)
from .protocol import (
    NOTIFY_UUID,
    STAGE_ACKS,
    STAGE_HOT_CLEARED,
    STAGE_READY,
    WRITE_UUID,
    Calibration,
    Frame,
    Init,
    Op,
    ProtocolError,
    build_frame,
    clamp,
    parse_frame,
)

_LOGGER = logging.getLogger(__name__)

#: Gap between the sequential calibration reads.  Blasting them back to back is
#: known to provoke an E04 bus fault on this controller family.
_INIT_GAP = 0.05

#: Calibration sub-registers we cannot work without.
_REQUIRED_INIT = (Init.BASE_HALL, Init.MIN_HALL, Init.MAX_HALL, Init.MODEL)

#: Shorter reply timeout for the fast sampling inside a move loop.
_MOVE_QUERY_TIMEOUT = 1.0

#: Hall counts of movement that count as real progress, for stall detection.
_PROGRESS_HALL = 2


class DeskError(Exception):
    """Base error for desk communication."""


class DeskNotFound(DeskError):
    """The desk was not visible to any Bluetooth adapter or proxy."""


class DeskNotConnected(DeskError):
    """A command was attempted while disconnected."""


class DeskTimeout(DeskError):
    """The desk did not reply in time."""


class DeskCalibrationError(DeskError):
    """Calibration is missing or unusable."""


@dataclass
class DeskState:
    """Everything the entities render, in one object."""

    connected: bool = False
    run_hall: int | None = None
    target_hall: int | None = None
    moving: bool = False
    direction: str | None = None
    error_code: int | None = None
    error_text: str | None = None
    calibration: Calibration | None = None

    @property
    def height_cm(self) -> float | None:
        """Current height in centimetres, or ``None`` if not yet known."""
        if self.calibration is None or self.run_hall is None:
            return None
        with contextlib.suppress(ProtocolError):
            return self.calibration.hall_to_cm(self.run_hall)
        return None

    @property
    def target_cm(self) -> float | None:
        """Height the current move is aiming for."""
        if self.calibration is None or self.target_hall is None:
            return None
        with contextlib.suppress(ProtocolError):
            return self.calibration.hall_to_cm(self.target_hall)
        return None

    @property
    def position(self) -> int | None:
        """Current height as 0-100, where 0 is the desk's lowest position."""
        if self.calibration is None or self.run_hall is None:
            return None
        with contextlib.suppress(ProtocolError):
            return self.calibration.hall_to_position(self.run_hall)
        return None


class UpDownDesk:
    """Owns the BLE link to one desk and speaks the PairLink protocol."""

    def __init__(
        self,
        name: str,
        address: str,
        device_provider: Callable[[], BLEDevice | None],
        *,
        calibration: Calibration | None = None,
    ) -> None:
        """Set up a desk.

        ``device_provider`` is called for every connection attempt so the link
        follows the desk between adapters and ESPHome proxies.
        """
        self.name = name
        self.address = address
        self.move_timeout: float = DEFAULT_MOVE_TIMEOUT
        #: Answer the desk's idle handshake with a height query.  Keeps the
        #: height live when someone uses the handset, at the cost of keeping
        #: the desk's LED display awake.
        self.follow_handshake: bool = True
        #: Seconds of inactivity before dropping the link, or ``None`` to hold
        #: it open (which locks out the phone app).
        self.idle_timeout: float | None = None

        self._device_provider = device_provider
        self._state = DeskState(calibration=calibration)
        self._client: BleakClientWithServiceCache | None = None
        self._write_char: BleakGATTCharacteristic | None = None
        self._write_response = False
        self._loop = asyncio.get_event_loop()

        # Guards the GATT write only, so a handshake ACK is never stuck behind
        # a request that is waiting for its reply.
        self._write_lock = asyncio.Lock()
        # Serialises whole exchanges: requests, moves, calibration reads.
        self._action_lock = asyncio.Lock()
        self._connect_lock = asyncio.Lock()

        self._waiters: dict[int, list[tuple[int | None, asyncio.Future[Frame]]]] = {}
        self._listeners: list[Callable[[], None]] = []
        self._dirty = False
        self._fault_seq = 0
        self._idle_handle: asyncio.TimerHandle | None = None
        self._background: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ state

    @property
    def state(self) -> DeskState:
        """The live state object."""
        return self._state

    @property
    def calibration(self) -> Calibration | None:
        """Cached calibration, if known."""
        return self._state.calibration

    def require_calibration(self) -> Calibration:
        """Return the calibration or raise if the desk has not reported it."""
        if (cal := self._state.calibration) is None:
            raise DeskCalibrationError("desk calibration is not known yet")
        return cal

    def add_listener(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Register a state change callback; returns an unsubscribe function."""
        self._listeners.append(callback)

        def _remove() -> None:
            if callback in self._listeners:
                self._listeners.remove(callback)

        return _remove

    def _flush(self) -> None:
        """Fire listeners if anything changed since the last flush."""
        if not self._dirty:
            return
        self._dirty = False
        for callback in list(self._listeners):
            callback()

    def _set_run_hall(self, value: int) -> None:
        if self._state.run_hall != value:
            self._state.run_hall = value
            self._dirty = True

    def _set_moving(self, moving: bool, direction: str | None) -> None:
        if (self._state.moving, self._state.direction) != (moving, direction):
            self._state.moving = moving
            self._state.direction = direction
            self._dirty = True

    def _set_connected(self, connected: bool) -> None:
        if self._state.connected != connected:
            self._state.connected = connected
            self._dirty = True

    # ------------------------------------------------------------- connection

    async def async_connect(self) -> None:
        """Connect, subscribe to notifications and learn the GATT layout."""
        async with self._connect_lock:
            if self._client is not None and self._client.is_connected:
                return

            device = self._device_provider()
            if device is None:
                raise DeskNotFound(
                    f"{self.name} ({self.address}) is not in range of any "
                    "Bluetooth adapter or proxy"
                )

            _LOGGER.debug("%s: connecting", self.name)
            try:
                client = await establish_connection(
                    BleakClientWithServiceCache,
                    device,
                    self.name,
                    disconnected_callback=self._handle_disconnect,
                    ble_device_callback=self._device_provider,
                    use_services_cache=True,
                )
            except (BleakError, TimeoutError) as err:
                raise DeskError(f"could not connect to {self.name}: {err}") from err

            write_char = client.services.get_characteristic(WRITE_UUID)
            notify_char = client.services.get_characteristic(NOTIFY_UUID)
            if write_char is None or notify_char is None:
                with contextlib.suppress(BleakError, TimeoutError):
                    await client.disconnect()
                raise DeskError(
                    f"{self.name} does not expose the ff01/ff02 characteristics; "
                    "this does not look like a PairLink desk controller"
                )

            # ff01 advertises both write modes; write-without-response avoids a
            # round trip per command, which matters in the move loop.
            self._write_response = "write-without-response" not in write_char.properties
            self._write_char = write_char

            try:
                await client.start_notify(notify_char, self._handle_notify)
            except (BleakError, TimeoutError) as err:
                with contextlib.suppress(BleakError, TimeoutError):
                    await client.disconnect()
                raise DeskError(f"could not subscribe to {self.name}: {err}") from err

            self._client = client
            self._set_connected(True)
            self._flush()
            _LOGGER.debug(
                "%s: connected (write_response=%s)", self.name, self._write_response
            )

        # Give the desk a moment to push any queued handshake frames.
        await asyncio.sleep(0.3)
        self._touch()

    async def async_disconnect(self) -> None:
        """Drop the link, releasing the desk for the phone app."""
        self._cancel_idle_timer()
        async with self._connect_lock:
            client, self._client = self._client, None
            self._write_char = None
            self._set_connected(False)
            self._flush()
            if client is not None:
                _LOGGER.debug("%s: disconnecting", self.name)
                with contextlib.suppress(BleakError, TimeoutError):
                    await client.disconnect()

    async def _ensure_connected(self) -> None:
        if self._client is None or not self._client.is_connected:
            await self.async_connect()

    def _handle_disconnect(self, _client: BleakClientWithServiceCache) -> None:
        """Bleak's disconnect callback."""
        _LOGGER.debug("%s: disconnected", self.name)
        self._client = None
        self._write_char = None
        self._set_connected(False)
        self._fail_waiters(DeskNotConnected(f"{self.name} disconnected"))
        self._flush()

    # -------------------------------------------------------- idle disconnect

    def _touch(self) -> None:
        """Restart the idle timer after activity."""
        self._cancel_idle_timer()
        if self.idle_timeout is None or self._client is None:
            return
        self._idle_handle = self._loop.call_later(self.idle_timeout, self._on_idle)

    def _cancel_idle_timer(self) -> None:
        if self._idle_handle is not None:
            self._idle_handle.cancel()
            self._idle_handle = None

    def _on_idle(self) -> None:
        self._idle_handle = None
        if self._state.moving or self._action_lock.locked():
            self._touch()
            return
        self._spawn(self.async_disconnect())

    # ------------------------------------------------------------- primitives

    def _spawn(self, coro) -> None:
        """Run a coroutine detached, keeping a reference so it is not GC'd."""
        task = self._loop.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _write_frame(self, op: int, p1: int = 1, data: int = 0) -> None:
        async with self._write_lock:
            client = self._client
            char = self._write_char
            if client is None or char is None or not client.is_connected:
                raise DeskNotConnected(f"{self.name} is not connected")
            frame = build_frame(op, p1, data)
            _LOGGER.debug(
                "%s: -> %s  op=%s p1=%s data=%s", self.name, frame.hex(" "), op, p1, data
            )
            try:
                await client.write_gatt_char(char, frame, response=self._write_response)
            except (BleakError, TimeoutError) as err:
                raise DeskError(f"write to {self.name} failed: {err}") from err

    async def _request(
        self,
        op: int,
        p1: int = 1,
        *,
        expect_p1: int | None = None,
        timeout: float = REQUEST_TIMEOUT,
        retries: int = REQUEST_RETRIES,
    ) -> Frame:
        """Send a command and wait for the desk's reply.

        Replies are matched on the opcode alone unless ``expect_p1`` is given:
        a height query sent with ``p1=1`` comes back with ``p1=6``.
        """
        for attempt in range(retries):
            future: asyncio.Future[Frame] = self._loop.create_future()
            self._waiters.setdefault(op, []).append((expect_p1, future))
            try:
                await self._write_frame(op, p1)
                return await asyncio.wait_for(future, timeout)
            except TimeoutError:
                _LOGGER.debug(
                    "%s: no reply to op=%s p1=%s (attempt %s/%s)",
                    self.name,
                    op,
                    p1,
                    attempt + 1,
                    retries,
                )
            finally:
                self._discard_waiter(op, future)
        raise DeskTimeout(f"{self.name} did not reply to op={op} p1={p1}")

    def _discard_waiter(self, op: int, future: asyncio.Future[Frame]) -> None:
        waiters = self._waiters.get(op)
        if not waiters:
            return
        self._waiters[op] = [entry for entry in waiters if entry[1] is not future]
        if not self._waiters[op]:
            del self._waiters[op]

    def _fail_waiters(self, err: Exception) -> None:
        for waiters in list(self._waiters.values()):
            for _, future in waiters:
                if not future.done():
                    future.set_exception(err)
        self._waiters.clear()

    # ---------------------------------------------------------- notifications

    def _handle_notify(self, _char: BleakGATTCharacteristic, data: bytearray) -> None:
        raw = bytes(data)
        try:
            frame = parse_frame(raw)
        except ProtocolError as err:
            _LOGGER.debug("%s: ignoring notification %s (%s)", self.name, raw.hex(" "), err)
            return

        if not frame.crc_valid:
            _LOGGER.debug("%s: CRC mismatch on %s (%s)", self.name, raw.hex(" "), frame)
        _LOGGER.debug("%s: <- %s  %s", self.name, raw.hex(" "), frame)

        if frame.is_error:
            self._handle_error(frame)
        elif frame.op == Op.STAGE:
            self._handle_stage(frame)
        elif frame.op in (Op.QUERY, Op.STOP):
            # A STOP reply carries the final position; a QUERY reply the live
            # one.  Either way the value is a run_hall.
            self._set_run_hall(frame.value)

        self._resolve(frame)
        self._flush()

    def _resolve(self, frame: Frame) -> None:
        waiters = self._waiters.get(frame.op)
        if not waiters:
            return
        remaining: list[tuple[int | None, asyncio.Future[Frame]]] = []
        for expect_p1, future in waiters:
            if future.done():
                continue
            if expect_p1 is None or expect_p1 == frame.p1:
                future.set_result(frame)
            else:
                remaining.append((expect_p1, future))
        if remaining:
            self._waiters[frame.op] = remaining
        else:
            self._waiters.pop(frame.op, None)

    def _handle_error(self, frame: Frame) -> None:
        code = frame.value
        text = None if code == 0 else frame.error_text
        if (self._state.error_code, self._state.error_text) != (code or None, text):
            self._state.error_code = code or None
            self._state.error_text = text
            self._dirty = True
        if code:
            self._fault_seq += 1
            _LOGGER.warning("%s reported %s", self.name, text)

    def _handle_stage(self, frame: Frame) -> None:
        """Answer the desk-initiated handshake.

        The desk walks a small state machine while idle and expects the client
        to acknowledge with ``stage + 1``.  Stage 2 means "ready, you may ask
        for the height".
        """
        stage = frame.value
        if (ack := STAGE_ACKS.get(stage)) is not None:
            self._spawn(self._write_quietly(Op.STAGE, 1, ack))
        elif stage == STAGE_READY:
            if self.follow_handshake or self._state.moving:
                self._spawn(self._write_quietly(Op.QUERY))
        elif stage == STAGE_HOT_CLEARED and self._state.error_code:
            self._state.error_code = None
            self._state.error_text = None
            self._dirty = True

    async def _write_quietly(self, op: int, p1: int = 1, data: int = 0) -> None:
        """Write without caring about the outcome (handshake housekeeping)."""
        with contextlib.suppress(DeskError):
            await self._write_frame(op, p1, data)

    # --------------------------------------------------------------- commands

    async def async_read_calibration(self) -> Calibration:
        """Walk ``Op.INIT`` and cache the desk's geometry."""
        async with self._action_lock:
            await self._ensure_connected()
            calibration = await self._read_calibration()
            self._touch()
            return calibration

    async def _read_calibration(self) -> Calibration:
        values: dict[int, int] = {}
        for p1 in range(1, 10):
            try:
                frame = await self._request(Op.INIT, p1, expect_p1=p1)
            except DeskTimeout:
                if p1 in _REQUIRED_INIT:
                    raise
                _LOGGER.debug("%s: no reply for init p1=%s, skipping", self.name, p1)
                continue
            values[p1] = frame.value
            await asyncio.sleep(_INIT_GAP)

        _LOGGER.debug("%s: calibration registers %s", self.name, values)
        if (run_hall := values.get(Init.RUN_HALL)) is not None:
            self._set_run_hall(run_hall)

        calibration = Calibration(
            base_hall=values[Init.BASE_HALL],
            min_hall=values[Init.MIN_HALL],
            max_hall=values[Init.MAX_HALL],
            model=values[Init.MODEL],
        )
        if calibration.max_hall <= calibration.min_hall:
            raise DeskCalibrationError(
                f"implausible travel range from {self.name}: "
                f"min={calibration.min_hall} max={calibration.max_hall}"
            )
        try:
            _ = calibration.counts_per_cm
        except ProtocolError as err:
            raise DeskCalibrationError(
                f"{self.name} reports model index {calibration.model}, which has no "
                "known hall-counts-per-cm; please open an issue with this value"
            ) from err

        self._state.calibration = calibration
        self._dirty = True
        self._flush()
        return calibration

    async def async_refresh(self) -> None:
        """Poll the current height."""
        async with self._action_lock:
            await self._ensure_connected()
            frame = await self._request(Op.QUERY)
            self._set_run_hall(frame.value)
            self._flush()
            self._touch()

    async def async_stop(self) -> None:
        """Stop the motors."""
        await self._ensure_connected()
        self._set_moving(False, None)
        self._flush()
        await self._safe_stop()
        self._touch()

    async def async_jog(self, direction: str, duration: float) -> None:
        """Hold UP or DOWN for ``duration`` seconds, then stop."""
        if direction not in ("up", "down"):
            raise ValueError(f"direction must be up or down, got {direction!r}")
        seconds = min(max(duration, 0.05), MAX_JOG_SECONDS)
        op = Op.UP if direction == "up" else Op.DOWN

        async with self._action_lock:
            await self._ensure_connected()
            self._set_moving(True, direction)
            self._flush()
            try:
                await self._write_frame(op)
                await asyncio.sleep(seconds)
            finally:
                self._set_moving(False, None)
                self._flush()
                await self._safe_stop()
            self._touch()

    async def async_recall_preset(self, op: Op) -> None:
        """Recall a stored preset and follow the desk until it settles."""
        async with self._action_lock:
            await self._ensure_connected()
            fault = self._fault_seq
            self._set_moving(True, None)
            self._flush()
            try:
                await self._write_frame(op)
                await self._track_until_idle(fault)
            finally:
                self._set_moving(False, None)
                self._flush()
                # The desk stops itself at the preset; this is belt and braces
                # and hands back the final position.
                await self._safe_stop()
            self._touch()

    async def async_move_to_hall(self, target: int) -> None:
        """Move to an absolute ``run_hall`` position and stop there."""
        async with self._action_lock:
            await self._ensure_connected()
            cal = self.require_calibration()
            target = clamp(target, cal.min_run_hall, cal.max_run_hall)
            self._state.target_hall = target
            self._dirty = True
            self._flush()
            try:
                for attempt in range(MOVE_MAX_PASSES):
                    if await self._move_pass(target, attempt):
                        break
            finally:
                self._state.target_hall = None
                self._set_moving(False, None)
                self._dirty = True
                self._flush()
            self._touch()

    async def async_move_to_cm(self, height_cm: float) -> None:
        """Move to a height in centimetres."""
        cal = self.require_calibration()
        await self.async_move_to_hall(cal.cm_to_hall(height_cm))

    async def async_move_to_position(self, position: int) -> None:
        """Move to a 0-100 position, 0 being the lowest height."""
        cal = self.require_calibration()
        await self.async_move_to_hall(cal.position_to_hall(position))

    # ------------------------------------------------------------ move engine

    async def _move_pass(self, target: int, attempt: int) -> bool:
        """Run one approach to ``target``; return True when close enough."""
        current = await self._sample_hall(strict=True)
        error = target - current
        if abs(error) <= DEADBAND_HALL:
            return True

        direction = "up" if error > 0 else "down"
        op = Op.UP if error > 0 else Op.DOWN
        fault = self._fault_seq
        samples: deque[tuple[float, int]] = deque(maxlen=5)
        samples.append((self._loop.time(), current))
        _LOGGER.debug(
            "%s: pass %s moving %s from %s to %s",
            self.name,
            attempt + 1,
            direction,
            current,
            target,
        )

        final: int | None = None
        self._set_moving(True, direction)
        self._flush()
        try:
            await self._write_frame(op)
            deadline = self._loop.time() + self.move_timeout
            last_hall = current
            last_progress = self._loop.time()

            while True:
                await asyncio.sleep(MOVE_POLL_INTERVAL)
                now = self._loop.time()

                if self._fault_seq != fault:
                    raise DeskError(
                        f"{self.name} reported {self._state.error_text} during the move"
                    )

                hall = await self._sample_hall()
                if hall is None:
                    if now > deadline:
                        raise DeskTimeout(f"{self.name} stopped reporting its height")
                    continue

                samples.append((now, hall))
                if abs(hall - last_hall) >= _PROGRESS_HALL:
                    last_hall, last_progress = hall, now

                lead = self._stop_lead(samples)
                if (op is Op.UP and hall >= target - lead) or (
                    op is Op.DOWN and hall <= target + lead
                ):
                    break

                if now - last_progress > STALL_TIMEOUT:
                    _LOGGER.warning(
                        "%s stopped making progress at %s (target %s); "
                        "it may be at a travel limit or obstructed",
                        self.name,
                        hall,
                        target,
                    )
                    break

                if now > deadline:
                    raise DeskTimeout(
                        f"{self.name} did not reach {target} within {self.move_timeout}s"
                    )
        finally:
            self._set_moving(False, None)
            self._flush()
            final = await self._safe_stop()

        if final is None:
            return True  # position unknown: do not blindly move again
        reached = abs(target - final) <= RETRY_THRESHOLD_HALL
        if not reached:
            _LOGGER.debug("%s: overshot to %s, target %s", self.name, final, target)
        return reached

    async def _track_until_idle(self, fault: int) -> None:
        """Follow a desk-driven move (preset recall) until the height settles."""
        deadline = self._loop.time() + self.move_timeout
        last_hall = self._state.run_hall
        last_change = self._loop.time()
        seen_motion = False

        while self._loop.time() < deadline:
            await asyncio.sleep(MOVE_POLL_INTERVAL)
            if self._fault_seq != fault:
                raise DeskError(f"{self.name} reported {self._state.error_text}")

            hall = await self._sample_hall()
            if hall is None:
                continue
            now = self._loop.time()

            if last_hall is not None and abs(hall - last_hall) >= _PROGRESS_HALL:
                direction = "up" if hall > last_hall else "down"
                self._set_moving(True, direction)
                self._flush()
                seen_motion = True
                last_change = now
            last_hall = hall

            if now - last_change > PRESET_SETTLE_SECONDS:
                if seen_motion:
                    return
                # Never moved: either already at the preset or it is unset.
                _LOGGER.debug("%s: preset produced no movement", self.name)
                return

    async def _sample_hall(self, *, strict: bool = False) -> int | None:
        """Read the height, falling back to the last pushed value.

        With ``strict`` a reading must be obtained, otherwise the move is
        abandoned before the motors are started.
        """
        try:
            frame = await self._request(
                Op.QUERY,
                timeout=_MOVE_QUERY_TIMEOUT if not strict else REQUEST_TIMEOUT,
                retries=1 if not strict else REQUEST_RETRIES,
            )
        except DeskTimeout:
            if strict:
                raise
            return self._state.run_hall
        self._set_run_hall(frame.value)
        return frame.value

    @staticmethod
    def _stop_lead(samples: deque[tuple[float, int]]) -> float:
        """Hall counts to stop short by, from the speed over recent samples."""
        if len(samples) < 2:
            return 0.0
        (first_t, first_h), (last_t, last_h) = samples[0], samples[-1]
        elapsed = last_t - first_t
        if elapsed <= 0:
            return 0.0
        speed = abs(last_h - first_h) / elapsed
        return speed * STOP_LEAD_SECONDS

    async def _safe_stop(self) -> int | None:
        """Send STOP, shielded from cancellation, and return the final height.

        This runs on every exit path from a move, so it must not raise.
        """
        try:
            return await asyncio.shield(self._loop.create_task(self._stop_and_read()))
        except asyncio.CancelledError:
            # The shielded STOP is already on its way; let cancellation through.
            raise
        except DeskError as err:
            _LOGGER.error("%s: could not confirm STOP: %s", self.name, err)
            return None

    async def _stop_and_read(self) -> int | None:
        """Write STOP (retrying hard) and return the position it reports."""
        last_error: Exception | None = None
        for _ in range(3):
            try:
                frame = await self._request(Op.STOP, timeout=1.0, retries=2)
            except DeskTimeout as err:
                # The writes went out even though no echo came back.
                last_error = err
                break
            except DeskNotConnected as err:
                raise DeskError(f"{self.name} disconnected before STOP") from err
            except DeskError as err:
                last_error = err
                await asyncio.sleep(0.1)
                continue
            self._set_run_hall(frame.value)
            self._flush()
            return frame.value

        if last_error is not None:
            _LOGGER.debug("%s: STOP not acknowledged (%s)", self.name, last_error)
        return self._state.run_hall
