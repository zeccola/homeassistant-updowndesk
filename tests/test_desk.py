"""Functional tests for the desk command layer, against a simulated desk.

These cover the parts that would move real furniture: the closed loop, and the
promise that every path which starts the motors also stops them.
"""

from __future__ import annotations

import asyncio

import pytest

from custom_components.updown_desk.desk import (
    DeskCalibrationError,
    DeskError,
    DeskNotFound,
    UpDownDesk,
)
from custom_components.updown_desk.protocol import Op

from . import fake_desk
from .fake_desk import FakeDesk

pytestmark = pytest.mark.asyncio


async def make_desk(monkeypatch, fake: FakeDesk, **kwargs) -> UpDownDesk:
    """Build a desk wired to the simulator and connect it."""
    fake_desk.install(monkeypatch, fake)
    desk = UpDownDesk("Test Desk", "EC:C5:7F:AE:26:7F", lambda: object(), **kwargs)
    desk.follow_handshake = False
    await desk.async_connect()
    return desk


class TestConnection:
    """Connecting, and refusing to talk to the wrong device."""

    async def test_connect_subscribes_and_reports_connected(self, monkeypatch) -> None:
        fake = FakeDesk()
        desk = await make_desk(monkeypatch, fake)
        assert desk.state.connected
        assert fake.is_connected

    async def test_missing_device_is_reported_clearly(self, monkeypatch) -> None:
        fake_desk.install(monkeypatch, FakeDesk())
        desk = UpDownDesk("Test Desk", "EC:C5:7F:AE:26:7F", lambda: None)
        with pytest.raises(DeskNotFound):
            await desk.async_connect()

    async def test_wrong_gatt_layout_is_rejected(self, monkeypatch) -> None:
        """A PairLink module without ff01/ff02 must not be adopted."""
        fake = FakeDesk(omit_characteristics=True)
        fake_desk.install(monkeypatch, fake)
        desk = UpDownDesk("Test Desk", "EC:C5:7F:AE:26:7F", lambda: object())
        with pytest.raises(DeskError, match="ff01/ff02"):
            await desk.async_connect()
        assert fake.disconnect_calls == 1

    async def test_write_without_response_is_preferred(self, monkeypatch) -> None:
        desk = await make_desk(monkeypatch, FakeDesk(write_without_response=True))
        assert desk._write_response is False

    async def test_falls_back_to_acknowledged_writes(self, monkeypatch) -> None:
        desk = await make_desk(monkeypatch, FakeDesk(write_without_response=False))
        assert desk._write_response is True


class TestQueries:
    """Reading height and calibration."""

    async def test_height_reply_with_mismatched_p1_is_accepted(self, monkeypatch) -> None:
        """The desk answers a p1=1 query with p1=6; that must not be missed."""
        fake = FakeDesk(run_hall=800)
        desk = await make_desk(monkeypatch, fake)
        await desk.async_refresh()
        assert desk.state.run_hall == 800

    async def test_calibration_walk_is_sequential(self, monkeypatch) -> None:
        """Init registers must be read one at a time to avoid an E04 fault."""
        fake = FakeDesk(run_hall=800)
        desk = await make_desk(monkeypatch, fake)
        calibration = await desk.async_read_calibration()

        assert (calibration.base_hall, calibration.max_hall) == (2816, 5691)
        assert calibration.model == 4
        assert calibration.counts_per_cm == 44.0
        init_p1s = [p1 for op, p1, _ in fake.commands if op == Op.INIT]
        assert init_p1s == list(range(1, 10))

    async def test_height_in_cm_matches_the_handset(self, monkeypatch) -> None:
        fake = FakeDesk(run_hall=800)
        desk = await make_desk(monkeypatch, fake)
        await desk.async_read_calibration()
        assert desk.state.height_cm == pytest.approx(82.18, abs=0.01)

    async def test_unknown_model_is_refused(self, monkeypatch) -> None:
        """Rather than report a wrong height, refuse the desk."""
        fake = FakeDesk()
        monkeypatch.setattr(fake_desk, "MODEL", 99, raising=False)
        monkeypatch.setitem(fake.__dict__, "_init_override", None)
        original = fake._init_value
        monkeypatch.setattr(fake, "_init_value", lambda p1: 99 if p1 == 9 else original(p1))
        desk = await make_desk(monkeypatch, fake)
        with pytest.raises(DeskCalibrationError, match="model index 99"):
            await desk.async_read_calibration()

    async def test_implausible_range_is_refused(self, monkeypatch) -> None:
        fake = FakeDesk()
        original = fake._init_value
        monkeypatch.setattr(
            fake, "_init_value", lambda p1: 100 if p1 == 7 else original(p1)
        )
        desk = await make_desk(monkeypatch, fake)
        with pytest.raises(DeskCalibrationError, match="implausible"):
            await desk.async_read_calibration()


class TestHandshake:
    """The desk-initiated staged handshake."""

    async def test_stages_are_acknowledged(self, monkeypatch) -> None:
        """Stage 0 must be answered with 1, or the desk keeps restarting."""
        fake = FakeDesk()
        await make_desk(monkeypatch, fake)
        fake.start_handshake(period=0.1)
        await asyncio.sleep(0.35)
        acks = [value for op, _, value in fake.commands if op == Op.STAGE]
        assert 1 in acks

    async def test_stage_two_triggers_a_query_when_following(self, monkeypatch) -> None:
        """With tracking on, "ready" is answered with a height query."""
        fake = FakeDesk(run_hall=900)
        desk = await make_desk(monkeypatch, fake)
        desk.follow_handshake = True
        fake.start_handshake(period=0.1)
        await asyncio.sleep(0.35)
        assert fake.count(Op.QUERY) >= 1
        assert desk.state.run_hall == 900

    async def test_no_query_when_not_following(self, monkeypatch) -> None:
        """During quiet hours the desk's display must be left dark."""
        fake = FakeDesk()
        desk = await make_desk(monkeypatch, fake)
        desk.follow_handshake = False
        fake.start_handshake(period=0.1)
        await asyncio.sleep(0.35)
        assert fake.count(Op.QUERY) == 0


class TestMovement:
    """The closed loop that stands in for the missing "go to height"."""

    @pytest.fixture
    async def ready(self, monkeypatch):
        fake = FakeDesk(run_hall=800)
        desk = await make_desk(monkeypatch, fake)
        await desk.async_read_calibration()
        return desk, fake

    async def test_moves_up_to_target_and_stops(self, ready) -> None:
        desk, fake = ready
        await desk.async_move_to_hall(900)
        assert fake.direction == 0
        assert abs(fake.run_hall - 900) <= 8, f"landed at {fake.run_hall}"
        assert fake.count(Op.UP) == 1

    async def test_moves_down_to_target_and_stops(self, ready) -> None:
        desk, fake = ready
        await desk.async_move_to_hall(700)
        assert fake.direction == 0
        assert abs(fake.run_hall - 700) <= 8, f"landed at {fake.run_hall}"
        assert fake.count(Op.DOWN) == 1
        assert fake.count(Op.UP) == 0

    async def test_move_to_cm_lands_on_the_right_height(self, ready) -> None:
        desk, fake = ready
        await desk.async_move_to_cm(85.0)
        assert fake.height_cm == pytest.approx(85.0, abs=0.25)

    async def test_target_already_reached_does_not_move(self, ready) -> None:
        """Inside the deadband the motors must stay off."""
        desk, fake = ready
        await desk.async_move_to_hall(801)
        assert fake.count(Op.UP) == 0
        assert fake.count(Op.DOWN) == 0

    async def test_target_beyond_travel_is_clamped(self, monkeypatch) -> None:
        """Asking for 200 cm must stop at the desk's own limit, not push into it."""
        fake = FakeDesk(run_hall=2800)
        desk = await make_desk(monkeypatch, fake)
        await desk.async_read_calibration()
        await desk.async_move_to_hall(99999)
        assert fake.run_hall == pytest.approx(2875, abs=8)
        assert fake.direction == 0

    async def test_position_move(self, ready) -> None:
        desk, fake = ready
        await desk.async_move_to_position(30)
        assert desk.state.calibration.hall_to_position(int(fake.run_hall)) == pytest.approx(
            30, abs=1
        )

    async def test_overshoot_is_corrected_by_a_second_pass(self, monkeypatch) -> None:
        """A desk that coasts a long way should still end up near the target."""
        fake = FakeDesk(run_hall=800, coast=40)
        desk = await make_desk(monkeypatch, fake)
        await desk.async_read_calibration()
        await desk.async_move_to_hall(900)
        assert fake.direction == 0
        # Two passes: the overshoot is walked back.
        assert fake.count(Op.UP) + fake.count(Op.DOWN) == 2
        assert abs(fake.run_hall - 900) <= 40

    async def test_stall_stops_the_motors(self, monkeypatch) -> None:
        """An obstruction must end the move rather than burn the motor."""
        fake = FakeDesk(run_hall=800, stall_above=850)
        desk = await make_desk(monkeypatch, fake)
        await desk.async_read_calibration()
        await desk.async_move_to_hall(2000)
        assert fake.direction == 0
        assert fake.stop_count >= 1

    async def test_timeout_stops_the_motors(self, monkeypatch) -> None:
        """A move that runs long is abandoned, with the motors stopped."""
        fake = FakeDesk(run_hall=0, speed=2.0)
        desk = await make_desk(monkeypatch, fake)
        await desk.async_read_calibration()
        desk.move_timeout = 0.5
        with pytest.raises(DeskError):
            await desk.async_move_to_hall(2800)
        assert fake.direction == 0
        assert fake.stop_count >= 1
        assert not desk.state.moving

    async def test_cancellation_still_stops_the_motors(self, ready) -> None:
        """The one that matters most: a cancelled move must not run on."""
        desk, fake = ready
        task = asyncio.create_task(desk.async_move_to_hall(2800))
        await asyncio.sleep(0.3)
        assert fake.direction != 0, "the desk should be moving by now"

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.1)  # let the shielded STOP land

        assert fake.stop_count >= 1
        assert fake.direction == 0
        assert not desk.state.moving

    async def test_fault_during_a_move_stops_the_motors(self, ready) -> None:
        """A thermal or overload report must abort the move."""
        desk, fake = ready
        task = asyncio.create_task(desk.async_move_to_hall(2800))
        await asyncio.sleep(0.25)
        fake.send_error(32)  # HOT

        with pytest.raises(DeskError, match="HOT"):
            await task
        assert fake.direction == 0
        assert fake.stop_count >= 1

    async def test_moving_state_is_exposed_while_travelling(self, ready) -> None:
        desk, _ = ready
        task = asyncio.create_task(desk.async_move_to_hall(900))
        await asyncio.sleep(0.25)
        assert desk.state.moving
        assert desk.state.direction == "up"
        assert desk.state.target_hall == 900
        await task
        assert not desk.state.moving
        assert desk.state.direction is None
        assert desk.state.target_hall is None

    async def test_streaming_desk_is_tracked(self, monkeypatch) -> None:
        """If the firmware does push heights, they must be believed."""
        fake = FakeDesk(run_hall=800, stream_while_moving=True)
        desk = await make_desk(monkeypatch, fake)
        await desk.async_read_calibration()
        await desk.async_move_to_hall(1000)
        assert abs(fake.run_hall - 1000) <= 8

    async def test_listeners_fire_on_height_change(self, ready) -> None:
        desk, _ = ready
        seen: list[int | None] = []
        desk.add_listener(lambda: seen.append(desk.state.run_hall))
        await desk.async_move_to_hall(900)
        assert len(seen) > 2
        assert seen[-1] is not None


class TestJog:
    """Manual nudging."""

    async def test_jog_up_then_stops(self, monkeypatch) -> None:
        fake = FakeDesk(run_hall=800)
        desk = await make_desk(monkeypatch, fake)
        await desk.async_jog("up", 0.5)
        assert fake.count(Op.UP) == 1
        assert fake.stop_count == 1
        assert fake.direction == 0
        assert fake.run_hall > 800

    async def test_jog_is_capped(self, monkeypatch) -> None:
        """A long hold must be clipped, per the safety notes."""
        fake = FakeDesk(run_hall=800, speed=1000)
        desk = await make_desk(monkeypatch, fake)
        loop = asyncio.get_running_loop()
        started = loop.time()
        await desk.async_jog("up", 60)
        assert loop.time() - started < 4.0

    async def test_jog_rejects_a_bad_direction(self, monkeypatch) -> None:
        desk = await make_desk(monkeypatch, FakeDesk())
        with pytest.raises(ValueError, match="direction"):
            await desk.async_jog("sideways", 0.5)

    async def test_cancelled_jog_stops(self, monkeypatch) -> None:
        fake = FakeDesk(run_hall=800)
        desk = await make_desk(monkeypatch, fake)
        task = asyncio.create_task(desk.async_jog("up", 3.0))
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.1)
        assert fake.stop_count >= 1
        assert fake.direction == 0


class TestPresets:
    """Preset recall, where the desk drives itself."""

    async def test_preset_is_followed_until_it_settles(self, monkeypatch) -> None:
        fake = FakeDesk(run_hall=800)
        desk = await make_desk(monkeypatch, fake)
        await desk.async_read_calibration()
        await desk.async_recall_preset(Op.PRESET_STAND)
        assert fake.run_hall == pytest.approx(1000, abs=8)
        assert not desk.state.moving

    async def test_preset_that_moves_down(self, monkeypatch) -> None:
        fake = FakeDesk(run_hall=900)
        desk = await make_desk(monkeypatch, fake)
        await desk.async_read_calibration()
        await desk.async_recall_preset(Op.PRESET_SIT)
        assert fake.run_hall == pytest.approx(700, abs=8)

    async def test_cancelled_preset_stops(self, monkeypatch) -> None:
        fake = FakeDesk(run_hall=800)
        desk = await make_desk(monkeypatch, fake)
        await desk.async_read_calibration()
        task = asyncio.create_task(desk.async_recall_preset(Op.PRESET_STAND))
        await asyncio.sleep(0.25)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.1)
        assert fake.direction == 0
        assert fake.stop_count >= 1


class TestStopAndDisconnect:
    """Stopping and releasing the desk."""

    async def test_stop_reports_the_final_height(self, monkeypatch) -> None:
        """The STOP acknowledgement carries the position; use it."""
        fake = FakeDesk(run_hall=812)
        desk = await make_desk(monkeypatch, fake)
        await desk.async_stop()
        assert desk.state.run_hall == 812

    async def test_disconnect_releases_the_desk(self, monkeypatch) -> None:
        """The desk allows one connection, so unloading must let go."""
        fake = FakeDesk()
        desk = await make_desk(monkeypatch, fake)
        await desk.async_disconnect()
        assert not fake.is_connected
        assert not desk.state.connected

    async def test_commands_after_disconnect_reconnect(self, monkeypatch) -> None:
        fake = FakeDesk(run_hall=800)
        desk = await make_desk(monkeypatch, fake)
        await desk.async_disconnect()
        await desk.async_refresh()
        assert desk.state.connected
        assert desk.state.run_hall == 800

    async def test_error_frames_are_recorded_and_cleared(self, monkeypatch) -> None:
        fake = FakeDesk()
        desk = await make_desk(monkeypatch, fake)
        fake.send_error(5)
        await asyncio.sleep(0.05)
        assert desk.state.error_code == 5
        assert desk.state.error_text == "E05 overload"

        fake.send_error(0)
        await asyncio.sleep(0.05)
        assert desk.state.error_code is None
        assert desk.state.error_text is None

    async def test_calibration_is_required_before_moving_in_cm(self, monkeypatch) -> None:
        """Without geometry, a centimetre target is meaningless."""
        desk = await make_desk(monkeypatch, FakeDesk())
        with pytest.raises(DeskCalibrationError):
            await desk.async_move_to_cm(90)
