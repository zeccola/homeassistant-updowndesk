# UpDown Desk for Home Assistant

Control an **UpDown Desk Pro Plus** standing desk over Bluetooth LE from Home
Assistant. Works with a local Bluetooth adapter or an ESPHome Bluetooth proxy.

The desk's controller is a PairLink module (the `Waltz` / UpDown app), the same
protocol the Ergostol desks use, so other PairLink desks may work too.

## What you get

| Entity | What it does |
|---|---|
| `cover.updown_desk_…` | Up, down, stop, and a 0–100% height slider |
| `number` Target height | Move to an exact height in cm |
| `sensor` Height | Current height in cm |
| `button` Sit / Middle / Stand | Recall the presets stored in the desk |
| `button` Stop | Stop immediately |
| `button` Nudge up / Nudge down | A 0.5 s tap for fine adjustment |
| `binary_sensor` Moving | On while the desk travels |
| `binary_sensor` Fault, `sensor` Last error | Controller errors (overload, thermal, …) |
| `binary_sensor` Bluetooth connection | Whether HA currently holds the link |

Plus the `updown_desk.jog` service, for holding a direction for a set time:

```yaml
action: updown_desk.jog
target:
  entity_id: cover.updown_desk_ae267f
data:
  direction: up
  duration: 1.5   # seconds, capped at 3
```

## Install

### HACS

1. HACS → three-dot menu → **Custom repositories**.
2. Add `https://github.com/zeccola/homeassistant-updowndesk`, category
   **Integration**.
3. Install **UpDown Desk**, then restart Home Assistant.

### Manually

Copy `custom_components/updown_desk/` into your `config/custom_components/`
directory and restart Home Assistant.

## Set up

**Close the phone app first.** The desk accepts only one Bluetooth connection at
a time, so the app and Home Assistant cannot both be connected.

The desk should be discovered automatically — look for a notification on the
**Devices & services** page. Otherwise add it with **+ Add integration → UpDown
Desk**. If nothing is listed, the flow will ask for the desk's Bluetooth address
directly.

Because the desk advertises a service UUID generic to all PairLink modules,
setup connects and reads the desk's calibration before creating anything. That
also caches the travel limits, so the height bounds match your desk rather than
being hardcoded.

## Options

Configure → **UpDown Desk** → **Configure**:

| Option | Default | Notes |
|---|---|---|
| Height poll interval | 30 s | How often to ask for the height. 0 disables polling. |
| Stay connected | on | Holds the Bluetooth link open. **This blocks the phone app.** Turn it off to connect only when a command is sent, releasing the desk after 20 s idle. |
| Track the height continuously | on | Answers the desk's idle handshake with a height query, so handset movements appear straight away. Keeps the desk's LED display awake. |
| Quiet hours | unset | Suspends background polling and tracking, so the display stays dark overnight. Set both times to enable. |
| Movement timeout | 60 s | Give up and stop if a move takes longer. |

If you want to keep using the phone app, turn **Stay connected** off.

## How "move to a height" works

The firmware has no "go to height X" command — only up, down, stop, and three
presets. So the integration holds the motor and watches the height, stopping
short by the distance the desk is expected to coast, then makes one corrective
pass if it overshot. Targets are clamped to the limits the desk itself reports.

Every code path that starts the motors ends in a STOP: on reaching the target,
on a controller fault, on a stall, on the timeout, on Home Assistant unloading
the integration, and on cancellation, where the STOP is shielded so it still
reaches the desk. This is the part the test suite covers most heavily.

## Safety

- **There is no authentication.** The desk accepts commands from anyone in
  Bluetooth range, with or without this integration. That is how the hardware
  ships; this integration cannot change it.
- Desks move heavy things. Keep cables, drawers and people clear, and check what
  is under and over the desk before automating it.
- Manual holds are capped at 3 seconds.
- The integration never writes the desk's calibration registers, which could
  otherwise leave it not knowing its own limits.

## Troubleshooting

**"Could not talk to the desk"** — the phone app is probably still connected.
Close it fully, then retry.

**Entities are unavailable** — the desk may be out of range of every adapter and
proxy, or something else holds the connection. With **Stay connected** off, brief
disconnections are normal and entities stay available.

**Heights look wrong** — enable debug logging and open an issue with the
calibration values. Add to `configuration.yaml`:

```yaml
logger:
  logs:
    custom_components.updown_desk: debug
```

That logs every frame in both directions. The **Hall counter** diagnostic sensor
(disabled by default) exposes the raw position and the calibration the desk
reported.

**A different PairLink desk** — it may well work. If setup reports an unsupported
model, the log line names the model index; open an issue with it.

## Development

```bash
pip install -r requirements-test.txt
python -m pytest            # 84 tests, no hardware needed
python -m ruff check .
```

The tests run against a desk simulator (`tests/fake_desk.py`) that speaks the
real wire protocol, including the CRC, the `p1=6` height replies, the idle
handshake and motor coast. `custom_components/updown_desk/protocol.py` is pure
and has no Home Assistant or bleak imports.

There is also a hardware CLI that needs only `bleak`:

```bash
python tools/desk_cli.py scan
python tools/desk_cli.py --address EC:C5:7F:AE:26:7F info
python tools/desk_cli.py --address EC:C5:7F:AE:26:7F listen 30
python tools/desk_cli.py --address EC:C5:7F:AE:26:7F goto 100
```

[`docs/PROTOCOL.md`](docs/PROTOCOL.md) documents the wire protocol, which
observations are verified against hardware, and what is still unconfirmed.

## Credits

The protocol notes build on
[samplec0de/homeassistant-ergostol](https://github.com/samplec0de/homeassistant-ergostol)
(MIT), which reverse engineered the PairLink protocol for the Ergostol desks.

## Licence

AGPL-3.0 — see [LICENSE](LICENSE).
