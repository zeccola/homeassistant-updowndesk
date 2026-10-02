"""Tests for the wire protocol and height maths."""

from __future__ import annotations

import pytest

from custom_components.updown_desk.protocol import (
    CRC_HEADER,
    CRC_HEADER_RX,
    Calibration,
    Op,
    ProtocolError,
    build_frame,
    crc16,
    parse_frame,
    truncate_cm,
)


def test_known_crc_vector_from_the_app() -> None:
    """The vector captured from the vendor app must reproduce exactly."""
    assert crc16(CRC_HEADER + bytes((0x50, 1, 0, 0))) == 0x15BA


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ((0x50,), "50 01 00 00 ba 15"),
        ((Op.QUERY,), "08 01 00 00 a9 75"),
        ((Op.STOP,), "09 01 00 00 a8 89"),
        ((Op.UP,), "02 01 00 00 aa ad"),
        ((Op.INIT, 5), "07 05 00 00 eb a0"),
    ],
)
def test_command_frames_match_captures(args, expected: str) -> None:
    """Every frame captured from the real desk must be byte identical."""
    assert build_frame(*args).hex(" ") == expected


@pytest.mark.parametrize(
    ("raw", "op", "p1", "value"),
    [
        ("08 06 03 20 d9 90", Op.QUERY, 6, 800),  # height reply
        ("02 01 00 00 6a a1", Op.UP, 1, 0),  # up echo
        ("09 01 03 2c 69 a8", Op.STOP, 1, 812),  # stop ack with final height
    ],
)
def test_captured_replies_parse_and_pass_crc(
    raw: str, op: int, p1: int, value: int
) -> None:
    """Replies recorded from the desk decode and verify against the RX header."""
    frame = parse_frame(bytes.fromhex(raw.replace(" ", "")))
    assert (frame.op, frame.p1, frame.value) == (op, p1, value)
    assert frame.crc_valid


def test_reply_and_command_share_a_body_but_not_a_crc() -> None:
    """The desk's echo of UP differs from the UP command only in its CRC.

    This is what gives away the separate inbound CRC header.
    """
    command = build_frame(Op.UP)
    echo = build_frame(Op.UP, header=CRC_HEADER_RX)
    assert command[:4] == echo[:4]
    assert command[4:] != echo[4:]
    assert echo.hex(" ") == "02 01 00 00 6a a1"


def test_error_frames_are_flagged() -> None:
    """A p1 of 0x80 marks an error report, not a value."""
    frame = parse_frame(build_frame(0, 0x80, 32, header=CRC_HEADER_RX))
    assert frame.is_error
    assert frame.error_text == "HOT (thermal protection)"


def test_cleared_error_is_not_a_fault_text() -> None:
    """Code 0 means the error went away."""
    frame = parse_frame(build_frame(0, 0x80, 0, header=CRC_HEADER_RX))
    assert frame.is_error
    assert frame.error_text == "cleared"


def test_short_frames_are_rejected() -> None:
    """Anything under six bytes cannot be a frame."""
    with pytest.raises(ProtocolError):
        parse_frame(b"\x08\x01\x00")


def test_corrupt_crc_is_reported_not_raised() -> None:
    """A bad CRC is surfaced as a flag so the frame can still be used."""
    frame = parse_frame(bytes.fromhex("080603200000"))
    assert not frame.crc_valid
    assert frame.value == 800


class TestCalibration:
    """The height conversion, checked against the physical handset."""

    @pytest.fixture
    def calibration(self) -> Calibration:
        return Calibration(base_hall=2816, min_hall=2816, max_hall=5691, model=4)

    def test_counts_per_cm_for_this_desk(self, calibration: Calibration) -> None:
        assert calibration.counts_per_cm == 44.0

    def test_height_matches_the_handset_display(self, calibration: Calibration) -> None:
        """run_hall 800 read 82.1 on the handset, which truncates."""
        assert calibration.hall_to_cm(800) == pytest.approx(82.1818, abs=1e-4)
        assert truncate_cm(calibration.hall_to_cm(800)) == 82.1

    def test_travel_range(self, calibration: Calibration) -> None:
        assert calibration.min_cm == pytest.approx(64.0)
        assert calibration.max_cm == pytest.approx(129.34, abs=0.01)
        assert calibration.min_run_hall == 0
        assert calibration.max_run_hall == 2875

    def test_cm_to_hall_round_trips(self, calibration: Calibration) -> None:
        for cm in (64.0, 82.1, 100.0, 120.5, 129.3):
            hall = calibration.cm_to_hall(cm)
            assert abs(calibration.hall_to_cm(hall) - cm) < 0.03

    def test_targets_are_clamped_to_the_desk_range(self, calibration: Calibration) -> None:
        """A request outside the desk's travel must not run the motors into it."""
        assert calibration.cm_to_hall(10) == calibration.min_run_hall
        assert calibration.cm_to_hall(500) == calibration.max_run_hall

    def test_position_maps_over_the_travel(self, calibration: Calibration) -> None:
        assert calibration.hall_to_position(0) == 0
        assert calibration.hall_to_position(2875) == 100
        assert calibration.hall_to_position(1438) == 50
        assert calibration.position_to_hall(0) == 0
        assert calibration.position_to_hall(100) == 2875

    def test_position_is_clamped(self, calibration: Calibration) -> None:
        assert calibration.hall_to_position(-500) == 0
        assert calibration.hall_to_position(9999) == 100

    def test_unknown_model_is_rejected(self) -> None:
        """An unknown model index must not silently produce wrong heights."""
        unknown = Calibration(base_hall=0, min_hall=0, max_hall=100, model=99)
        with pytest.raises(ProtocolError):
            _ = unknown.counts_per_cm

    def test_model_byte_is_masked(self) -> None:
        """Only the low byte of the model word selects the geometry."""
        assert Calibration(0, 0, 100, model=0x0104).counts_per_cm == 44.0


def test_truncate_matches_handset_rounding() -> None:
    """The handset truncates rather than rounds."""
    assert truncate_cm(82.18) == 82.1
    assert truncate_cm(82.99) == 82.9
    assert truncate_cm(64.0) == 64.0
