"""Wire protocol for PairLink standing desk controllers (UpDown Pro Plus).

This module is deliberately free of Home Assistant and bleak imports so it can
be unit tested on its own.  See ``docs/PROTOCOL.md`` for the reverse
engineering notes this implements.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
import math
from typing import Final

SERVICE_UUID: Final = "0000ff12-0000-1000-8000-00805f9b34fb"
WRITE_UUID: Final = "0000ff01-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID: Final = "0000ff02-0000-1000-8000-00805f9b34fb"

FRAME_LENGTH: Final = 6

#: Constant prefix included in the CRC of an outbound frame but never put on
#: the wire.  The last byte appears to identify the sender, so replies are
#: covered by :data:`CRC_HEADER_RX` instead -- an echoed command carries the
#: same four body bytes as the command but a different CRC.
CRC_HEADER: Final = bytes((0x04, 0xFC, 0x42, 0x06))

#: Same, for frames sent by the desk.  Recovered by solving for the CRC state
#: that satisfies the captured replies; see ``docs/PROTOCOL.md``.
CRC_HEADER_RX: Final = bytes((0x04, 0xFC, 0x42, 0x56))

_CRC_TABLE: Final = (
    0,
    52225,
    55297,
    5120,
    61441,
    15360,
    10240,
    58369,
    40961,
    27648,
    30720,
    46081,
    20480,
    39937,
    34817,
    17408,
)

#: ``p1`` value that marks a notification as an error report.
ERROR_P1: Final = 0x80


class Op(IntEnum):
    """Opcodes understood by the controller."""

    DOWN = 1
    UP = 2
    PRESET_STAND = 3
    PRESET_MIDDLE = 4
    PRESET_SIT = 5
    CONFIG = 6
    INIT = 7
    QUERY = 8
    STOP = 9
    STAGE = 11
    HEARTBEAT = 12


class Init(IntEnum):
    """``p1`` sub-registers of :attr:`Op.INIT` (calibration read)."""

    UNIT = 4
    BASE_HALL = 5
    MIN_HALL = 6
    MAX_HALL = 7
    RUN_HALL = 8
    MODEL = 9


#: Stages of the desk-initiated handshake that expect ``stage + 1`` back.
STAGE_ACKS: Final = {0: 1, 5: 6, 9: 10}

#: Stage that means "ready, the client may now query the height".
STAGE_READY: Final = 2

#: Stage that means the desk has left thermal protection.
STAGE_HOT_CLEARED: Final = 11

ERRORS: Final = {
    0: "cleared",
    1: "E01 motor stopped",
    2: "E02 out of sync",
    3: "E03 cable",
    4: "E04 controller bus comms",
    5: "E05 overload",
    32: "HOT (thermal protection)",
}

#: Hall counts per centimetre, indexed by the model byte from ``Op.INIT`` p1=9.
COUNTS_PER_CM: Final = {
    1: 29.333334,
    2: 29.333334,
    3: 11.0,
    4: 44.0,
    5: 26.0,
    6: 58.666668,
    7: 29.8,
    8: 26.0,
    9: 27.5,
    10: 44.0,
    11: 22.0,
}


class ProtocolError(ValueError):
    """Raised when a byte string is not a valid frame."""


def crc16(buf: bytes) -> int:
    """Return the controller's nibble-wise CRC-16 over ``buf``."""
    crc = 0xFFFF
    for byte in buf:
        crc = ((crc >> 4) ^ _CRC_TABLE[(crc & 0xF) ^ (byte & 0xF)]) & 0xFFFF
        crc = ((crc >> 4) ^ _CRC_TABLE[(crc & 0xF) ^ ((byte >> 4) & 0xF)]) & 0xFFFF
    return crc


def build_frame(
    op: int, p1: int = 1, data: int = 0, *, header: bytes = CRC_HEADER
) -> bytes:
    """Build a six byte frame, CRC appended little-endian.

    ``header`` selects the direction; the default builds a frame to send to the
    desk.  Pass :data:`CRC_HEADER_RX` to build one the desk would send.
    """
    body = bytes((op & 0xFF, p1 & 0xFF, (data >> 8) & 0xFF, data & 0xFF))
    crc = crc16(header + body)
    return body + bytes((crc & 0xFF, crc >> 8))


@dataclass(frozen=True, slots=True)
class Frame:
    """A decoded notification or command frame."""

    op: int
    p1: int
    value: int
    crc_valid: bool = True

    @property
    def is_error(self) -> bool:
        """Whether this frame reports an error rather than a value."""
        return self.p1 == ERROR_P1

    @property
    def error_text(self) -> str | None:
        """Human readable error, or ``None`` when this is not an error frame."""
        if not self.is_error:
            return None
        return ERRORS.get(self.value, f"unknown error {self.value}")

    def __str__(self) -> str:
        """Render the frame for logs."""
        name = Op(self.op).name.lower() if self.op in Op.__members__.values() else self.op
        if self.is_error:
            return f"error {self.error_text}"
        return f"op={name} p1={self.p1} val={self.value}"


def parse_frame(data: bytes, *, header: bytes = CRC_HEADER_RX) -> Frame:
    """Decode a frame.

    ``header`` selects the direction; the default decodes a frame sent by the
    desk.  Pass :data:`CRC_HEADER` to decode one sent to it.

    The CRC is checked but a mismatch is reported through
    :attr:`Frame.crc_valid` rather than raising.  It has only been confirmed
    against one firmware revision, and BLE already checks integrity at the link
    layer, so a mismatch is worth logging but not worth dropping the frame for.
    """
    if len(data) < FRAME_LENGTH:
        raise ProtocolError(f"short frame: {data.hex(' ')}")
    op, p1, hi, lo = data[0], data[1], data[2], data[3]
    expected = crc16(header + bytes((op, p1, hi, lo)))
    actual = data[4] | (data[5] << 8)
    return Frame(op=op, p1=p1, value=(hi << 8) | lo, crc_valid=expected == actual)


@dataclass(frozen=True, slots=True)
class Calibration:
    """Per-desk geometry read from ``Op.INIT``.

    ``base_hall``, ``min_hall`` and ``max_hall`` are absolute hall counts,
    while the position reported by :attr:`Op.QUERY` (``run_hall``) is relative
    to ``base_hall``.
    """

    base_hall: int
    min_hall: int
    max_hall: int
    model: int

    @property
    def counts_per_cm(self) -> float:
        """Hall counts per centimetre for this model."""
        try:
            return COUNTS_PER_CM[self.model & 0xFF]
        except KeyError as err:
            raise ProtocolError(f"unknown desk model index {self.model}") from err

    @property
    def min_run_hall(self) -> int:
        """Lowest reachable ``run_hall``."""
        return self.min_hall - self.base_hall

    @property
    def max_run_hall(self) -> int:
        """Highest reachable ``run_hall``."""
        return self.max_hall - self.base_hall

    @property
    def min_cm(self) -> float:
        """Lowest reachable height in centimetres."""
        return self.min_hall / self.counts_per_cm

    @property
    def max_cm(self) -> float:
        """Highest reachable height in centimetres."""
        return self.max_hall / self.counts_per_cm

    def hall_to_cm(self, run_hall: int) -> float:
        """Convert a ``run_hall`` reading to centimetres."""
        return (run_hall + self.base_hall) / self.counts_per_cm

    def cm_to_hall(self, cm: float) -> int:
        """Convert centimetres to a ``run_hall`` target, clamped to the range."""
        raw = round(cm * self.counts_per_cm) - self.base_hall
        return clamp(raw, self.min_run_hall, self.max_run_hall)

    def hall_to_position(self, run_hall: int) -> int:
        """Map ``run_hall`` onto 0-100, where 0 is the lowest height."""
        span = self.max_run_hall - self.min_run_hall
        if span <= 0:
            return 0
        pct = (run_hall - self.min_run_hall) * 100 / span
        return clamp(round(pct), 0, 100)

    def position_to_hall(self, position: int) -> int:
        """Map a 0-100 position onto a ``run_hall`` target."""
        span = self.max_run_hall - self.min_run_hall
        return clamp(
            self.min_run_hall + round(span * position / 100),
            self.min_run_hall,
            self.max_run_hall,
        )


def clamp(value: int, low: int, high: int) -> int:
    """Clamp ``value`` into ``[low, high]``."""
    return max(low, min(high, value))


def truncate_cm(cm: float) -> float:
    """Truncate to one decimal place, the way the desk's handset displays it."""
    return math.floor(cm * 10) / 10
