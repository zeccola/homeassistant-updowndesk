"""Config flow for the UpDown Desk integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_ble_device_from_address,
    async_discovered_service_info,
)
from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import callback
from homeassistant.helpers import selector
import voluptuous as vol

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
)
from .desk import (
    DeskCalibrationError,
    DeskError,
    DeskNotFound,
    UpDownDesk,
)
from .protocol import SERVICE_UUID, Calibration

_LOGGER = logging.getLogger(__name__)

MAC_PATTERN = vol.Match(
    r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$",
    msg="Expected a MAC address such as EC:C5:7F:AE:26:7F",
)


def suggested_title(address: str) -> str:
    """Name a desk after the tail of its MAC, as the app does."""
    tail = "".join(address.split(":")[-3:]).upper()
    return f"UpDown Desk {tail}" if tail else "UpDown Desk"


class UpDownDeskConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle discovery and manual setup of a desk."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialise the flow."""
        self._discovery: BluetoothServiceInfoBleak | None = None

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle a desk found by the Bluetooth integration."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        self._discovery = discovery_info
        self.context["title_placeholders"] = {
            "name": suggested_title(discovery_info.address)
        }
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm a discovered desk.

        The ff12 service is generic to PairLink modules, so nothing is created
        until the desk answers a calibration read.
        """
        assert self._discovery is not None
        address = self._discovery.address
        errors: dict[str, str] = {}

        if user_input is not None:
            calibration, error = await self._async_probe(address)
            if calibration is not None:
                return self._async_create(address, calibration)
            errors["base"] = error or "cannot_connect"

        return self.async_show_form(
            step_id="confirm",
            errors=errors,
            description_placeholders={
                "name": self._discovery.name or suggested_title(address),
                "address": address,
            },
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick a desk from the ones Bluetooth has seen."""
        errors: dict[str, str] = {}

        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            calibration, error = await self._async_probe(address)
            if calibration is not None:
                return self._async_create(address, calibration)
            errors["base"] = error or "cannot_connect"

        candidates = self._async_candidates()
        if not candidates:
            return await self.async_step_manual()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_ADDRESS): vol.In(candidates)}),
            errors=errors,
        )

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Enter a desk's MAC address by hand."""
        errors: dict[str, str] = {}

        if user_input is not None:
            address = user_input[CONF_ADDRESS].upper()
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            calibration, error = await self._async_probe(address)
            if calibration is not None:
                return self._async_create(address, calibration)
            errors["base"] = error or "cannot_connect"

        return self.async_show_form(
            step_id="manual",
            data_schema=vol.Schema({vol.Required(CONF_ADDRESS): MAC_PATTERN}),
            errors=errors,
        )

    @callback
    def _async_candidates(self) -> dict[str, str]:
        """Discovered PairLink controllers that are not configured yet."""
        configured = self._async_current_ids()
        candidates: dict[str, str] = {}
        for info in async_discovered_service_info(self.hass, connectable=True):
            if info.address in configured:
                continue
            if SERVICE_UUID not in info.service_uuids:
                continue
            candidates[info.address] = f"{info.name or 'Desk'} ({info.address})"
        return candidates

    async def _async_probe(self, address: str) -> tuple[Calibration | None, str | None]:
        """Connect and read calibration, to prove this really is a desk."""
        device = async_ble_device_from_address(self.hass, address, connectable=True)
        if device is None:
            return None, "not_found"

        desk = UpDownDesk(
            suggested_title(address),
            address,
            lambda: async_ble_device_from_address(self.hass, address, connectable=True),
        )
        try:
            await desk.async_connect()
            return await desk.async_read_calibration(), None
        except DeskNotFound:
            return None, "not_found"
        except DeskCalibrationError as err:
            _LOGGER.warning("Unsupported desk at %s: %s", address, err)
            return None, "unsupported_model"
        except DeskError as err:
            _LOGGER.debug("Probe of %s failed: %s", address, err)
            return None, "cannot_connect"
        finally:
            await desk.async_disconnect()

    @callback
    def _async_create(self, address: str, calibration: Calibration) -> ConfigFlowResult:
        """Create the entry, caching the geometry we just read."""
        return self.async_create_entry(
            title=suggested_title(address),
            data={
                CONF_ADDRESS: address,
                CONF_BASE_HALL: calibration.base_hall,
                CONF_MIN_HALL: calibration.min_hall,
                CONF_MAX_HALL: calibration.max_hall,
                CONF_MODEL: calibration.model,
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> OptionsFlow:
        """Return the options flow."""
        return UpDownDeskOptionsFlow()


class UpDownDeskOptionsFlow(OptionsFlow):
    """Tune polling and connection behaviour."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            # Absent time fields mean "no quiet hours"; keep them out entirely.
            return self.async_create_entry(
                data={key: value for key, value in user_input.items() if value != ""}
            )

        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_POLL_INTERVAL,
                    default=options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0,
                        max=3600,
                        step=5,
                        unit_of_measurement="s",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    CONF_KEEP_CONNECTED,
                    default=options.get(CONF_KEEP_CONNECTED, DEFAULT_KEEP_CONNECTED),
                ): selector.BooleanSelector(),
                vol.Required(
                    CONF_FOLLOW_HANDSHAKE,
                    default=options.get(CONF_FOLLOW_HANDSHAKE, DEFAULT_FOLLOW_HANDSHAKE),
                ): selector.BooleanSelector(),
                vol.Optional(
                    CONF_QUIET_START,
                    description={"suggested_value": options.get(CONF_QUIET_START)},
                ): selector.TimeSelector(),
                vol.Optional(
                    CONF_QUIET_END,
                    description={"suggested_value": options.get(CONF_QUIET_END)},
                ): selector.TimeSelector(),
                vol.Required(
                    CONF_MOVE_TIMEOUT,
                    default=options.get(CONF_MOVE_TIMEOUT, DEFAULT_MOVE_TIMEOUT),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=5,
                        max=300,
                        step=5,
                        unit_of_measurement="s",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
