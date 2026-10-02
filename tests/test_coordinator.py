"""Tests for the coordinator's scheduling logic."""

from __future__ import annotations

from datetime import time

from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.updown_desk.const import (
    CONF_FOLLOW_HANDSHAKE,
    CONF_KEEP_CONNECTED,
    CONF_POLL_INTERVAL,
    CONF_QUIET_END,
    CONF_QUIET_START,
    DOMAIN,
    IDLE_DISCONNECT_SECONDS,
)
from custom_components.updown_desk.coordinator import UpDownDeskCoordinator

ADDRESS = "EC:C5:7F:AE:26:7F"


def make_coordinator(hass: HomeAssistant, options: dict) -> UpDownDeskCoordinator:
    """Build a coordinator without connecting to anything."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ADDRESS: ADDRESS},
        options=options,
        unique_id=ADDRESS,
        title="UpDown Desk",
    )
    entry.add_to_hass(hass)
    return UpDownDeskCoordinator(hass, entry)


class TestQuietHours:
    """Quiet hours keep the desk's LED display dark overnight."""

    @pytest.mark.parametrize(
        ("now", "quiet"),
        [
            (time(22, 30), True),  # inside, before midnight
            (time(3, 0), True),  # inside, after midnight
            (time(6, 59), True),  # last minute inside
            (time(7, 0), False),  # the end is exclusive
            (time(12, 0), False),  # the middle of the day
            (time(21, 59), False),  # a minute before it starts
        ],
    )
    def test_window_wrapping_midnight(
        self, hass: HomeAssistant, now: time, quiet: bool
    ) -> None:
        coordinator = make_coordinator(
            hass, {CONF_QUIET_START: "22:00:00", CONF_QUIET_END: "07:00:00"}
        )
        assert coordinator._quiet_start == time(22, 0)
        assert coordinator._in_quiet_hours_at(now) is quiet

    @pytest.mark.parametrize(
        ("now", "quiet"),
        [
            (time(9, 0), False),
            (time(13, 0), True),
            (time(14, 0), False),
        ],
    )
    def test_window_within_one_day(
        self, hass: HomeAssistant, now: time, quiet: bool
    ) -> None:
        coordinator = make_coordinator(
            hass, {CONF_QUIET_START: "12:00:00", CONF_QUIET_END: "14:00:00"}
        )
        assert coordinator._in_quiet_hours_at(now) is quiet

    def test_unset_means_never_quiet(self, hass: HomeAssistant) -> None:
        coordinator = make_coordinator(hass, {})
        assert coordinator._in_quiet_hours_at(time(3, 0)) is False

    def test_half_set_means_never_quiet(self, hass: HomeAssistant) -> None:
        """One bound on its own does not define a window."""
        coordinator = make_coordinator(hass, {CONF_QUIET_START: "22:00:00"})
        assert coordinator._in_quiet_hours_at(time(23, 0)) is False


class TestOptions:
    """Options reach the desk object."""

    def test_keep_connected_holds_the_link_open(self, hass: HomeAssistant) -> None:
        coordinator = make_coordinator(hass, {CONF_KEEP_CONNECTED: True})
        assert coordinator.desk.idle_timeout is None

    def test_on_demand_releases_the_desk_when_idle(self, hass: HomeAssistant) -> None:
        """Turning it off lets the phone app have the desk back."""
        coordinator = make_coordinator(hass, {CONF_KEEP_CONNECTED: False})
        assert coordinator.desk.idle_timeout == IDLE_DISCONNECT_SECONDS

    def test_polling_can_be_disabled(self, hass: HomeAssistant) -> None:
        coordinator = make_coordinator(hass, {CONF_POLL_INTERVAL: 0})
        assert coordinator.update_interval is None

    def test_poll_interval_is_applied(self, hass: HomeAssistant) -> None:
        coordinator = make_coordinator(hass, {CONF_POLL_INTERVAL: 45})
        assert coordinator.update_interval.total_seconds() == 45

    def test_quiet_hours_suppress_handshake_tracking(self, hass: HomeAssistant) -> None:
        """During quiet hours the desk must not be prodded for its height."""
        coordinator = make_coordinator(
            hass,
            {
                CONF_FOLLOW_HANDSHAKE: True,
                CONF_QUIET_START: "00:00:00",
                CONF_QUIET_END: "23:59:59",
            },
        )
        coordinator._apply_quiet_state()
        assert coordinator.desk.follow_handshake is False
