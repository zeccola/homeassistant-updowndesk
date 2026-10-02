"""A simulated PairLink desk controller, for testing without hardware.

The simulator answers on the wire exactly as the captures in
``docs/PROTOCOL.md`` show: height replies come back with ``p1=6``, frames it
sends carry the desk-side CRC header, and the motors coast a little after STOP.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from custom_components.updown_desk.protocol import (
    CRC_HEADER,
    CRC_HEADER_RX,
    ERROR_P1,
    NOTIFY_UUID,
    WRITE_UUID,
    Init,
    Op,
    build_frame,
    parse_frame,
)

#: Counts per second, from the capture: 12 counts in a 0.5 s press.
SPEED_COUNTS_PER_SEC = 24.0

#: Counts the desk keeps travelling after a STOP is received.
COAST_COUNTS = 4

#: Model 4 geometry, as read off the real desk.
BASE_HALL = 2816
MIN_HALL = 2816
MAX_HALL = 5691
MODEL = 4


class FakeCharacteristic:
    """Stand-in for a bleak characteristic."""

    def __init__(self, uuid: str, properties: list[str]) -> None:
        self.uuid = uuid
        self.properties = properties


class FakeServices:
    """Stand-in for a bleak service collection."""

    def __init__(self, chars: dict[str, FakeCharacteristic]) -> None:
        self._chars = chars

    def get_characteristic(self, uuid: str) -> FakeCharacteristic | None:
        return self._chars.get(uuid)


class FakeDesk:
    """Simulates the controller's behaviour and its BLE client."""

    def __init__(
        self,
        *,
        run_hall: int = 800,
        speed: float = SPEED_COUNTS_PER_SEC,
        coast: int = COAST_COUNTS,
        stream_while_moving: bool = False,
        write_without_response: bool = True,
        stall_above: int | None = None,
        omit_characteristics: bool = False,
    ) -> None:
        """Set up the simulator.

        ``stream_while_moving`` makes the desk push unsolicited height frames,
        as some firmware revisions may.  ``stall_above`` simulates an
        obstruction: the desk refuses to travel past that position.
        """
        self.run_hall = float(run_hall)
        self.speed = speed
        self.coast = coast
        self.stream_while_moving = stream_while_moving
        self.write_without_response = write_without_response
        self.stall_above = stall_above
        self.omit_characteristics = omit_characteristics

        self.direction: int = 0  # +1 up, -1 down, 0 idle
        self.is_connected = False
        self.commands: list[tuple[int, int, int]] = []
        self.stop_count = 0
        self.disconnect_calls = 0
        # Kept close together so tests do not sit through 30 s of travel at
        # the desk's real speed.
        self.presets = {Op.PRESET_SIT: 700, Op.PRESET_MIDDLE: 850, Op.PRESET_STAND: 1000}
        self.preset_target: int | None = None

        self._notify: Callable[[object, bytearray], None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._last_tick: float = 0.0
        self._motion_task: asyncio.Task | None = None
        self._handshake_task: asyncio.Task | None = None
        self._char = FakeCharacteristic(
            WRITE_UUID,
            ["write", "write-without-response"] if write_without_response else ["write"],
        )
        chars = (
            {}
            if omit_characteristics
            else {
                WRITE_UUID: self._char,
                NOTIFY_UUID: FakeCharacteristic(NOTIFY_UUID, ["notify"]),
            }
        )
        self.services = FakeServices(chars)

    # ------------------------------------------------------ bleak client API

    async def connect(self) -> None:
        self.is_connected = True
        self._loop = asyncio.get_running_loop()
        self._last_tick = self._loop.time()

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.is_connected = False
        for task in (self._motion_task, self._handshake_task):
            if task is not None:
                task.cancel()
        self._motion_task = self._handshake_task = None

    async def start_notify(self, _char, callback) -> None:
        self._notify = callback

    async def stop_notify(self, _char) -> None:
        self._notify = None

    async def write_gatt_char(self, _char, data: bytes, response: bool = False) -> None:
        if not self.is_connected:
            raise RuntimeError("write while disconnected")
        frame = parse_frame(data, header=CRC_HEADER)
        assert frame.crc_valid, f"controller rejected CRC on {bytes(data).hex(' ')}"
        self.commands.append((frame.op, frame.p1, frame.value))
        await asyncio.sleep(0)  # let the write yield, as a real one does
        self._dispatch(frame.op, frame.p1, frame.value)

    # ------------------------------------------------------- desk behaviour

    def _dispatch(self, op: int, p1: int, value: int) -> None:
        self._advance()
        if op == Op.QUERY:
            self._send(Op.QUERY, 6, self._hall)
        elif op == Op.INIT:
            self._send(Op.INIT, p1, self._init_value(p1))
        elif op in (Op.UP, Op.DOWN):
            self.direction = 1 if op == Op.UP else -1
            self.preset_target = None
            self._send(op, 1, 0)
            self._start_motion()
        elif op == Op.STOP:
            self.stop_count += 1
            if self.direction:
                self.run_hall = self._clamp(self.run_hall + self.direction * self.coast)
            self.direction = 0
            self.preset_target = None
            self._send(Op.STOP, 1, self._hall)
        elif op in self.presets:
            target = self.presets[op]
            self.preset_target = target
            self.direction = 1 if target > self.run_hall else -1
            self._send(op, 1, 0)
            self._start_motion()
        elif op == Op.STAGE and value == 1:
            # The client acknowledged stage 0; answer with "ready".
            self._send(Op.STAGE, 1, 2)

    def _init_value(self, p1: int) -> int:
        return {
            1: 0,
            2: 0,
            3: 0,
            Init.UNIT: 1,
            Init.BASE_HALL: BASE_HALL,
            Init.MIN_HALL: MIN_HALL,
            Init.MAX_HALL: MAX_HALL,
            Init.RUN_HALL: self._hall,
            Init.MODEL: MODEL,
        }.get(p1, 0)

    @property
    def _hall(self) -> int:
        return round(self.run_hall)

    @property
    def height_cm(self) -> float:
        return (self.run_hall + BASE_HALL) / 44.0

    def _clamp(self, value: float) -> float:
        low, high = MIN_HALL - BASE_HALL, MAX_HALL - BASE_HALL
        if self.stall_above is not None:
            high = min(high, self.stall_above)
        return max(low, min(high, value))

    def _advance(self) -> None:
        """Integrate motion up to now."""
        if self._loop is None:
            return
        now = self._loop.time()
        elapsed, self._last_tick = now - self._last_tick, now
        if not self.direction or elapsed <= 0:
            return
        self.run_hall = self._clamp(self.run_hall + self.direction * self.speed * elapsed)
        if self.preset_target is not None:
            # The desk stops itself at a preset.
            reached = (
                self.run_hall >= self.preset_target
                if self.direction > 0
                else self.run_hall <= self.preset_target
            )
            if reached:
                self.run_hall = float(self.preset_target)
                self.direction = 0
                self.preset_target = None

    def _start_motion(self) -> None:
        if self._motion_task is None or self._motion_task.done():
            self._motion_task = asyncio.get_running_loop().create_task(self._motion_loop())

    async def _motion_loop(self) -> None:
        while self.is_connected:
            await asyncio.sleep(0.05)
            self._advance()
            if not self.direction:
                return
            if self.stream_while_moving:
                self._send(Op.QUERY, 6, self._hall)

    def _send(self, op: int, p1: int, value: int) -> None:
        if self._notify is None:
            return
        self._notify(None, bytearray(build_frame(op, p1, value, header=CRC_HEADER_RX)))

    # --------------------------------------------------------- test helpers

    def start_handshake(self, period: float = 0.3) -> None:
        """Begin the idle handshake loop the real desk runs."""
        self._handshake_task = asyncio.get_running_loop().create_task(
            self._handshake_loop(period)
        )

    async def _handshake_loop(self, period: float) -> None:
        while self.is_connected:
            self._send(Op.STAGE, 1, 0)
            await asyncio.sleep(period)

    def send_error(self, code: int) -> None:
        """Push an error report, as the desk does on a fault."""
        self._send(0, ERROR_P1, code)

    def count(self, op: int) -> int:
        """How many times an opcode was written to the desk."""
        return sum(1 for entry in self.commands if entry[0] == op)


def install(monkeypatch, fake: FakeDesk) -> None:
    """Point the integration's connection helper at ``fake``."""
    from custom_components.updown_desk import desk as desk_module

    async def _establish(_client_class, _device, _name, **kwargs):
        await fake.connect()
        if (callback := kwargs.get("disconnected_callback")) is not None:
            fake.on_disconnect = callback  # type: ignore[attr-defined]
        return fake

    monkeypatch.setattr(desk_module, "establish_connection", _establish)
