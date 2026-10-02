"""Constants for the UpDown Desk integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "updown_desk"

MANUFACTURER: Final = "UpDown Desk"
MODEL: Final = "Pro Plus (PairLink)"

# --- config entry data -------------------------------------------------------
CONF_BASE_HALL: Final = "base_hall"
CONF_MIN_HALL: Final = "min_hall"
CONF_MAX_HALL: Final = "max_hall"
CONF_MODEL: Final = "model"

# --- options -----------------------------------------------------------------
CONF_POLL_INTERVAL: Final = "poll_interval"
CONF_KEEP_CONNECTED: Final = "keep_connected"
CONF_FOLLOW_HANDSHAKE: Final = "follow_handshake"
CONF_QUIET_START: Final = "quiet_start"
CONF_QUIET_END: Final = "quiet_end"
CONF_MOVE_TIMEOUT: Final = "move_timeout"

DEFAULT_POLL_INTERVAL: Final = 30
DEFAULT_KEEP_CONNECTED: Final = True
DEFAULT_FOLLOW_HANDSHAKE: Final = True
DEFAULT_MOVE_TIMEOUT: Final = 60

# --- movement tuning ---------------------------------------------------------
#: How often to sample the height while a closed loop move is running.
MOVE_POLL_INTERVAL: Final = 0.15

#: Seconds of travel to stop short by, to absorb the desk's own stopping
#: distance.  Multiplied by the measured speed, so it adapts to the model.
STOP_LEAD_SECONDS: Final = 0.2

#: Hall counts (~44/cm on model 4) within which a target counts as reached.
DEADBAND_HALL: Final = 4

#: Error above which a second, corrective pass is attempted.
RETRY_THRESHOLD_HALL: Final = 8

#: Maximum number of passes for one closed loop move.
MOVE_MAX_PASSES: Final = 2

#: Abandon a move if the height has not changed for this long.
STALL_TIMEOUT: Final = 3.0

#: Hard cap on a manual jog, per the safety notes.
MAX_JOG_SECONDS: Final = 3.0
DEFAULT_JOG_SECONDS: Final = 0.5

#: Seconds to wait for a reply to a request, and how many times to resend.
REQUEST_TIMEOUT: Final = 2.0
REQUEST_RETRIES: Final = 3

#: Idle seconds before dropping the link when ``keep_connected`` is off.
IDLE_DISCONNECT_SECONDS: Final = 20.0

#: A preset recall is finished once the height has been static this long.
PRESET_SETTLE_SECONDS: Final = 1.5

SERVICE_JOG: Final = "jog"
ATTR_DIRECTION: Final = "direction"
ATTR_DURATION: Final = "duration"
