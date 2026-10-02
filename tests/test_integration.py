"""Home Assistant side tests: config flow, entity setup and services."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from homeassistant.components.cover import (
    ATTR_CURRENT_POSITION,
    ATTR_POSITION,
    DOMAIN as COVER_DOMAIN,
    SERVICE_CLOSE_COVER,
    SERVICE_OPEN_COVER,
    SERVICE_SET_COVER_POSITION,
    SERVICE_STOP_COVER,
)
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID, CONF_ADDRESS, STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.helpers import device_registry as dr, entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
import voluptuous as vol

from custom_components.updown_desk.const import (
    CONF_BASE_HALL,
    CONF_MAX_HALL,
    CONF_MIN_HALL,
    CONF_MODEL,
    CONF_POLL_INTERVAL,
    DOMAIN,
)
from custom_components.updown_desk.protocol import Op

from . import fake_desk
from .fake_desk import FakeDesk

ADDRESS = "EC:C5:7F:AE:26:7F"

ENTRY_DATA = {
    CONF_ADDRESS: ADDRESS,
    CONF_BASE_HALL: 2816,
    CONF_MIN_HALL: 2816,
    CONF_MAX_HALL: 5691,
    CONF_MODEL: 4,
}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations, socket_enabled, hass):
    """Load the integration from custom_components, with Bluetooth stubbed.

    The integration only reaches Bluetooth through
    ``async_ble_device_from_address``, which every test replaces, so marking the
    dependencies as set up avoids needing a real adapter.
    """
    hass.config.components.add("bluetooth")
    hass.config.components.add("bluetooth_adapters")
    return enable_custom_integrations


@pytest.fixture
def fake() -> FakeDesk:
    """Return a simulated desk sitting at 82.1 cm."""
    return FakeDesk(run_hall=800)


@pytest.fixture
def wired(monkeypatch, fake: FakeDesk) -> FakeDesk:
    """Point both the transport and Bluetooth discovery at the simulator."""
    fake_desk.install(monkeypatch, fake)
    monkeypatch.setattr(
        "custom_components.updown_desk.coordinator.bluetooth.async_ble_device_from_address",
        lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr(
        "custom_components.updown_desk.config_flow.async_ble_device_from_address",
        lambda *args, **kwargs: object(),
    )
    return fake


async def setup_entry(hass: HomeAssistant, options: dict | None = None) -> MockConfigEntry:
    """Add and set up a config entry."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=ENTRY_DATA,
        options=options or {CONF_POLL_INTERVAL: 0},
        unique_id=ADDRESS,
        title="UpDown Desk AE267F",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


class TestConfigFlow:
    """Discovery and manual setup."""

    async def test_user_flow_creates_entry(self, hass: HomeAssistant, wired) -> None:
        with patch(
            "custom_components.updown_desk.config_flow.async_discovered_service_info",
            return_value=[],
        ):
            result = await hass.config_entries.flow.async_init(
                DOMAIN, context={"source": SOURCE_USER}
            )
        # With nothing discovered the flow offers manual entry.
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "manual"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ADDRESS: ADDRESS}
        )
        await hass.async_block_till_done()

        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["title"] == "UpDown Desk AE267F"
        # The geometry read during the probe is cached on the entry.
        assert result["data"][CONF_BASE_HALL] == 2816
        assert result["data"][CONF_MAX_HALL] == 5691
        assert result["data"][CONF_MODEL] == 4

    async def test_manual_flow_rejects_a_bad_address(
        self, hass: HomeAssistant, wired
    ) -> None:
        with patch(
            "custom_components.updown_desk.config_flow.async_discovered_service_info",
            return_value=[],
        ):
            result = await hass.config_entries.flow.async_init(
                DOMAIN, context={"source": SOURCE_USER}
            )
        with pytest.raises(InvalidData):
            await hass.config_entries.flow.async_configure(
                result["flow_id"], {CONF_ADDRESS: "not-a-mac"}
            )

    async def test_unreachable_desk_shows_an_error(
        self, hass: HomeAssistant, monkeypatch, fake
    ) -> None:
        """A desk out of range must report that, not create a broken entry."""
        fake_desk.install(monkeypatch, fake)
        monkeypatch.setattr(
            "custom_components.updown_desk.config_flow.async_ble_device_from_address",
            lambda *args, **kwargs: None,
        )
        with patch(
            "custom_components.updown_desk.config_flow.async_discovered_service_info",
            return_value=[],
        ):
            result = await hass.config_entries.flow.async_init(
                DOMAIN, context={"source": SOURCE_USER}
            )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ADDRESS: ADDRESS}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"base": "not_found"}

    async def test_duplicate_is_rejected(self, hass: HomeAssistant, wired) -> None:
        await setup_entry(hass)
        with patch(
            "custom_components.updown_desk.config_flow.async_discovered_service_info",
            return_value=[],
        ):
            result = await hass.config_entries.flow.async_init(
                DOMAIN, context={"source": SOURCE_USER}
            )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ADDRESS: ADDRESS}
        )
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "already_configured"

    async def test_options_flow(self, hass: HomeAssistant, wired) -> None:
        entry = await setup_entry(hass)
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["type"] is FlowResultType.FORM

        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                CONF_POLL_INTERVAL: 60,
                "keep_connected": False,
                "follow_handshake": True,
                "move_timeout": 45,
            },
        )
        await hass.async_block_till_done()
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert entry.options[CONF_POLL_INTERVAL] == 60


class TestSetup:
    """Entity creation and teardown."""

    async def test_entities_are_created(self, hass: HomeAssistant, wired) -> None:
        entry = await setup_entry(hass)
        assert entry.state is ConfigEntryState.LOADED

        registry = er.async_get(hass)
        entities = {
            entity.entity_id
            for entity in er.async_entries_for_config_entry(registry, entry.entry_id)
        }
        expected = {
            "cover.updown_desk_ae267f",
            "sensor.updown_desk_ae267f_height",
            "sensor.updown_desk_ae267f_last_error",
            "number.updown_desk_ae267f_target_height",
            "binary_sensor.updown_desk_ae267f_moving",
            "binary_sensor.updown_desk_ae267f_fault",
            "binary_sensor.updown_desk_ae267f_bluetooth_connection",
            "button.updown_desk_ae267f_sit_preset",
            "button.updown_desk_ae267f_middle_preset",
            "button.updown_desk_ae267f_stand_preset",
            "button.updown_desk_ae267f_stop",
            "button.updown_desk_ae267f_nudge_up",
            "button.updown_desk_ae267f_nudge_down",
        }
        assert expected <= entities, f"missing: {expected - entities}"

    async def test_device_is_registered_with_its_mac(
        self, hass: HomeAssistant, wired
    ) -> None:
        await setup_entry(hass)
        device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, ADDRESS)})
        assert device is not None
        assert (dr.CONNECTION_BLUETOOTH, ADDRESS) in device.connections
        assert device.manufacturer == "UpDown Desk"

    async def test_height_is_reported_in_cm(self, hass: HomeAssistant, wired) -> None:
        await setup_entry(hass)
        state = hass.states.get("sensor.updown_desk_ae267f_height")
        assert state is not None
        assert float(state.state) == 82.1
        assert state.attributes["unit_of_measurement"] == "cm"

    async def test_target_height_bounds_come_from_the_desk(
        self, hass: HomeAssistant, wired
    ) -> None:
        await setup_entry(hass)
        state = hass.states.get("number.updown_desk_ae267f_target_height")
        assert state is not None
        assert state.attributes["min"] == 64.0
        assert state.attributes["max"] == 129.0

    async def test_cover_position_reflects_the_height(
        self, hass: HomeAssistant, wired
    ) -> None:
        await setup_entry(hass)
        state = hass.states.get("cover.updown_desk_ae267f")
        assert state is not None
        assert state.attributes[ATTR_CURRENT_POSITION] == 28

    async def test_unload_releases_the_desk(
        self, hass: HomeAssistant, wired: FakeDesk
    ) -> None:
        """The desk allows one connection, so unloading must disconnect."""
        entry = await setup_entry(hass)
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.NOT_LOADED
        assert not wired.is_connected

    async def test_calibration_is_read_and_cached_when_absent(
        self, hass: HomeAssistant, wired: FakeDesk
    ) -> None:
        """An entry with no cached geometry learns it and stores it."""
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={CONF_ADDRESS: ADDRESS},
            options={CONF_POLL_INTERVAL: 0},
            unique_id=ADDRESS,
            title="UpDown Desk AE267F",
        )
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert entry.data[CONF_BASE_HALL] == 2816
        assert entry.data[CONF_MIN_HALL] == 2816
        assert entry.data[CONF_MAX_HALL] == 5691
        assert entry.data[CONF_MODEL] == 4
        assert float(hass.states.get("sensor.updown_desk_ae267f_height").state) == 82.1

    async def test_cached_calibration_skips_the_init_walk(
        self, hass: HomeAssistant, wired: FakeDesk
    ) -> None:
        """A restart should not re-read the desk's geometry."""
        await setup_entry(hass)
        assert wired.count(Op.INIT) == 0

    async def test_setup_retries_when_the_desk_is_away(
        self, hass: HomeAssistant, monkeypatch, fake
    ) -> None:
        """An absent desk should schedule a retry, not fail permanently."""
        fake_desk.install(monkeypatch, fake)
        monkeypatch.setattr(
            "custom_components.updown_desk.coordinator.bluetooth.async_ble_device_from_address",
            lambda *args, **kwargs: None,
        )
        entry = MockConfigEntry(
            domain=DOMAIN, data=ENTRY_DATA, unique_id=ADDRESS, title="UpDown Desk"
        )
        entry.add_to_hass(hass)
        assert not await hass.config_entries.async_setup(entry.entry_id)
        assert entry.state is ConfigEntryState.SETUP_RETRY


class TestControl:
    """Driving the desk through Home Assistant services."""

    async def test_set_position_moves_the_desk(
        self, hass: HomeAssistant, wired: FakeDesk
    ) -> None:
        await setup_entry(hass)
        await hass.services.async_call(
            COVER_DOMAIN,
            SERVICE_SET_COVER_POSITION,
            {ATTR_ENTITY_ID: "cover.updown_desk_ae267f", ATTR_POSITION: 30},
            blocking=True,
        )
        await wait_until_idle(hass, wired)
        # 30% of 0..2875 is 862 hall counts.
        assert abs(wired.run_hall - 862) <= 10, f"landed at {wired.run_hall}"

    async def test_number_set_value_moves_the_desk(
        self, hass: HomeAssistant, wired: FakeDesk
    ) -> None:
        await setup_entry(hass)
        await hass.services.async_call(
            "number",
            "set_value",
            {
                ATTR_ENTITY_ID: "number.updown_desk_ae267f_target_height",
                "value": 83.0,
            },
            blocking=True,
        )
        await wait_until_idle(hass, wired)
        assert wired.height_cm == pytest.approx(83.0, abs=0.25)

    async def test_stop_button_halts_a_move(
        self, hass: HomeAssistant, wired: FakeDesk
    ) -> None:
        """The stop button must interrupt a move in progress."""
        await setup_entry(hass)
        await hass.services.async_call(
            COVER_DOMAIN,
            SERVICE_OPEN_COVER,
            {ATTR_ENTITY_ID: "cover.updown_desk_ae267f"},
            blocking=True,
        )
        await asyncio.sleep(0.3)
        assert wired.direction == 1, "the desk should be rising"

        await hass.services.async_call(
            "button",
            "press",
            {ATTR_ENTITY_ID: "button.updown_desk_ae267f_stop"},
            blocking=True,
        )
        await hass.async_block_till_done()
        assert wired.direction == 0
        assert wired.run_hall < 2875, "it must not have reached the top"

    async def test_stop_cover_halts_a_move(
        self, hass: HomeAssistant, wired: FakeDesk
    ) -> None:
        await setup_entry(hass)
        await hass.services.async_call(
            COVER_DOMAIN,
            SERVICE_CLOSE_COVER,
            {ATTR_ENTITY_ID: "cover.updown_desk_ae267f"},
            blocking=True,
        )
        await asyncio.sleep(0.3)
        assert wired.direction == -1

        await hass.services.async_call(
            COVER_DOMAIN,
            SERVICE_STOP_COVER,
            {ATTR_ENTITY_ID: "cover.updown_desk_ae267f"},
            blocking=True,
        )
        await hass.async_block_till_done()
        assert wired.direction == 0
        assert wired.run_hall > 0

    async def test_moving_state_is_published(
        self, hass: HomeAssistant, wired: FakeDesk
    ) -> None:
        await setup_entry(hass)
        assert hass.states.get("binary_sensor.updown_desk_ae267f_moving").state == STATE_OFF

        await hass.services.async_call(
            COVER_DOMAIN,
            SERVICE_OPEN_COVER,
            {ATTR_ENTITY_ID: "cover.updown_desk_ae267f"},
            blocking=True,
        )
        await asyncio.sleep(0.3)
        await hass.async_block_till_done()
        assert hass.states.get("binary_sensor.updown_desk_ae267f_moving").state == STATE_ON
        assert hass.states.get("cover.updown_desk_ae267f").state == "opening"

        await hass.services.async_call(
            COVER_DOMAIN,
            SERVICE_STOP_COVER,
            {ATTR_ENTITY_ID: "cover.updown_desk_ae267f"},
            blocking=True,
        )
        await hass.async_block_till_done()
        assert hass.states.get("binary_sensor.updown_desk_ae267f_moving").state == STATE_OFF

    async def test_jog_service(self, hass: HomeAssistant, wired: FakeDesk) -> None:
        await setup_entry(hass)
        start = wired.run_hall
        await hass.services.async_call(
            DOMAIN,
            "jog",
            {
                ATTR_ENTITY_ID: "cover.updown_desk_ae267f",
                "direction": "up",
                "duration": 0.5,
            },
            blocking=True,
        )
        await wait_until_idle(hass, wired)
        assert wired.run_hall > start
        assert wired.direction == 0

    async def test_jog_duration_is_capped(
        self, hass: HomeAssistant, wired: FakeDesk
    ) -> None:
        """The service schema must refuse an unsafe hold."""
        await setup_entry(hass)
        with pytest.raises(vol.Invalid):
            await hass.services.async_call(
                DOMAIN,
                "jog",
                {
                    ATTR_ENTITY_ID: "cover.updown_desk_ae267f",
                    "direction": "up",
                    "duration": 30,
                },
                blocking=True,
            )

    async def test_preset_button(self, hass: HomeAssistant, wired: FakeDesk) -> None:
        await setup_entry(hass)
        await hass.services.async_call(
            "button",
            "press",
            {ATTR_ENTITY_ID: "button.updown_desk_ae267f_stand_preset"},
            blocking=True,
        )
        await wait_until_idle(hass, wired, timeout=20)
        assert wired.run_hall == pytest.approx(1000, abs=10)

    async def test_fault_is_surfaced(self, hass: HomeAssistant, wired: FakeDesk) -> None:
        await setup_entry(hass)
        wired.send_error(32)
        await hass.async_block_till_done()
        assert hass.states.get("binary_sensor.updown_desk_ae267f_fault").state == STATE_ON
        assert (
            hass.states.get("sensor.updown_desk_ae267f_last_error").state
            == "HOT (thermal protection)"
        )


async def wait_until_idle(
    hass: HomeAssistant, fake: FakeDesk, timeout: float = 15.0
) -> None:
    """Wait for the desk to stop moving."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        await asyncio.sleep(0.1)
        if fake.direction == 0 and fake.stop_count:
            await hass.async_block_till_done()
            return
    raise AssertionError("the desk never stopped")
