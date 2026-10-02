# PairLink desk protocol (UpDown Pro Plus)

Notes for the BLE protocol this integration implements. The desk is an UpDown
Desk Pro Plus (Australia) with the Pro Plus controller, whose module is made by
PairLink; the vendor app is `com.pairlink.waltz.updown`. The same protocol is
used by the Ergostol desk (`com.pairlink.ergostol`), and
[samplec0de/homeassistant-ergostol](https://github.com/samplec0de/homeassistant-ergostol)
was the starting point for these notes.

Everything below marked **verified** was confirmed against real hardware. The
test suite encodes the captures as fixtures, so a regression shows up as a
failing test rather than a moving desk.

## Device identity

| Field | Value |
|---|---|
| Advertised name | `BLE Device-AE267F` (generic; don't match on name) |
| Primary service | `0000ff12-0000-1000-8000-00805f9b34fb` |
| Commands | `0000ff01-…` — write, write-without-response |
| Events | `0000ff02-…` — notify |
| Pairing | none; commands are accepted immediately |
| Connections | one at a time, so Home Assistant locks out the phone app |

Other characteristics (`ff03`, `ff04`, `ff06`, `ff08`, `ff0a`, `fff0`, `fff1`)
and the `00006287-…` / `0000d0ff-…` services are unused, and are probably
PairLink's OTA and config channels.

Because `ff12` is generic to PairLink modules, the config flow confirms a device
by connecting and reading its calibration before creating an entry.

## Frame format

Six bytes each way:

```
[op, p1, dataHi, dataLo, crcLo, crcHi]
```

`data` is a 16-bit big-endian value; the CRC is little-endian.

### CRC-16

The CRC covers a four-byte header that is **never transmitted**, followed by the
four body bytes. It is a nibble-table CRC-16 seeded with `0xFFFF`:

```python
CRC_HEADER = bytes((0x04, 0xFC, 0x42, 0x06))  # frames we send
CRC_HEADER_RX = bytes((0x04, 0xFC, 0x42, 0x56))  # frames the desk sends
```

**The two directions use different headers**, differing only in the last byte
(`0x06` vs `0x56`), which looks like a sender identifier. The giveaway is in the
captures: the desk's echo of an UP command has the same four body bytes as the
command but a different CRC.

```
-> 02 01 00 00 aa ad     UP command
<- 02 01 00 00 6a a1     the desk's echo: same body, different CRC
```

`CRC_HEADER_RX` was recovered by solving for the 16-bit CRC state that satisfies
all three captured replies; exactly one value fits, and `04 FC 42 56` is the
only header of the form `04 FC ?? ??` that produces it. With it, inbound frames
verify, so corruption is detectable in both directions. A mismatch is logged
rather than dropped: it is confirmed against one firmware revision only, and BLE
already checks integrity at the link layer.

### Test vectors (verified)

| Frame | Bytes |
|---|---|
| `build_frame(0x50)` | `50 01 00 00 ba 15` |
| `build_frame(8)` query | `08 01 00 00 a9 75` |
| `build_frame(9)` stop | `09 01 00 00 a8 89` |
| `build_frame(2)` up | `02 01 00 00 aa ad` |
| `build_frame(7, 5)` init p1=5 | `07 05 00 00 eb a0` |

Captured replies, all of which pass the inbound CRC:

| Bytes | Meaning |
|---|---|
| `08 06 03 20 d9 90` | height reply, `run_hall` = 800 |
| `02 01 00 00 6a a1` | UP echo |
| `09 01 03 2c 69 a8` | STOP ack, final `run_hall` = 812 |

## Opcodes

| op | Meaning | Status |
|---|---|---|
| 1 | move DOWN (until STOP) | assumed symmetric with UP |
| 2 | move UP (until STOP) | **verified** |
| 3 | recall STAND preset | untested on hardware |
| 4 | recall MIDDLE preset | untested on hardware |
| 5 | recall SIT preset | untested on hardware |
| 6 | config/save — **never sent by this integration** | see warning below |
| 7 | read calibration, one value per `p1` | **verified** |
| 8 | query height | **verified** |
| 9 | STOP | **verified** |
| 11 | staged handshake, desk-initiated | **verified** |
| 12 | heartbeat | not observed; not used |

> **Never send op 6 with `p1` 5, 6 or 7.** Those are calibration writes and can
> leave the desk not knowing where its own limits are. `p1` 1–3 save the current
> height as a preset and `p1` 4 switches the display between cm and inches; this
> integration sends none of them.

### Replies match on the opcode, not on `p1`

A height query sent with `p1=1` is answered with `p1=6`:

```
-> 08 01 00 00 a9 75     query
<- 08 06 03 20 d9 90     op=8 p1=6 val=800
```

Matching on `(op, p1)` therefore misses every height reply. The integration
matches on the opcode alone, except for the `op 7` walk, where `p1` identifies
which register came back. A test covers this.

### Errors (`p1 == 0x80`)

| Code | Meaning |
|---|---|
| 0 | cleared |
| 1 | E01 motor stopped |
| 2 | E02 out of sync (>15 mm) |
| 3 | E03 cable |
| 4 | E04 controller bus comms |
| 5 | E05 overload |
| 32 | HOT — thermal protection; stop and wait ~5 minutes |

A fault raised mid-move aborts the move and stops the motors.

## Calibration (op 7)

Read the registers **one at a time, waiting for each reply**. Sending them back
to back is reported to provoke an E04 bus fault.

```
p1=1 -> 0        p1=2 -> 0        p1=3 -> 0
p1=4 -> 1        possibly the cm/inch setting
p1=5 -> 2816     base_hall
p1=6 -> 2816     min_hall  (absolute)
p1=7 -> 5691     max_hall  (absolute)
p1=8 -> 800      run_hall  (current, relative to base)
p1=9 -> 4        model index
```

`base_hall`, `min_hall` and `max_hall` are absolute counts, while the position
reported by op 8 is relative to `base_hall`.

## Height conversion (verified against the handset)

```
cm       = (run_hall + base_hall) / counts_per_cm[model]
run_hall = round(cm * counts_per_cm[model]) - base_hall
```

```python
COUNTS_PER_CM = {
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
```

This desk is model 4, so 44 counts/cm. `run_hall = 800` gives
`(800 + 2816) / 44 = 82.1818 cm`, and the handset showed **82.1** — it
**truncates** rather than rounds, so the integration displays
`math.floor(cm * 10) / 10` to agree with it. Travel is 64.0–129.3 cm.

Only the low byte of the model word selects the geometry. An unknown index is
refused during setup rather than guessed at, since a wrong divisor would report
confidently wrong heights.

## Movement

```
-> 02 01 00 00 aa ad     UP
<- 02 01 00 00 6a a1     echo
   (0.5 s)
-> 09 01 00 00 a8 89     STOP
<- 09 01 03 2c 69 a8     final run_hall = 812
```

A 0.5 s press moved 12 counts, about 2.7 mm, so roughly **24 counts/s** or
5.5 mm/s. The STOP reply carries the final position and is used to update state.

No op-8 stream appeared during that short move. Whether the firmware streams
heights during longer moves is still open, so the integration polls op 8 every
150 ms while moving and also believes any pushed frames.

### Reaching a height

The firmware has no "go to X", so the integration closes the loop itself:

1. read the current `run_hall`;
2. clamp the target into `[min_hall - base_hall, max_hall - base_hall]`;
3. hold UP or DOWN;
4. sample every 150 ms, estimating speed from recent samples;
5. STOP when within `speed × 0.2 s` of the target, to absorb the coast;
6. if the result is more than 8 counts off, make one corrective pass.

It gives up, having stopped the motors, on a fault, a stall of 3 s, or the
movement timeout. **Every** exit path sends STOP, including cancellation, where
the STOP is shielded so it still reaches the desk.

## Staged handshake (op 11)

While idle the desk walks a small state machine and expects acknowledgements:

```
<- 0b 01 00 00     stage 0
-> 0b 01 00 01     ACK with stage + 1
<- 0b 01 00 02     stage 2: ready, the client may query the height
```

Stages 0, 5 and 9 are acknowledged with `stage + 1`. Stage 2 means ready, and
the vendor app answers it with a height query; stage 7 means the base/min/max
sync finished, and stage 11 means the desk left thermal protection.

Answering stage 2 keeps the height live when someone uses the handset, but it
also keeps the desk's LED display awake, which is why it is an option and is
suspended during quiet hours.

## Still open

Things worth confirming with `tools/desk_cli.py` on real hardware:

1. Does DOWN behave symmetrically to UP? (assumed, not measured)
2. Does the desk stream op-8 frames during a long move?
3. Does answering stage 2 stop the handshake loop from restarting?
4. Do handset-driven moves produce notifications once the handshake completes?
5. Do preset recalls (ops 3/4/5) work, and do they report progress?
6. What does op 7 `p1=4` (value 1) mean? cm/inch, most likely.
7. Is a heartbeat (op 12) needed to hold the connection open?

The integration is written so that a "no" to any of 2–4 costs accuracy or
latency, never safety: it polls rather than relying on a stream, and it stops
the motors on every exit path.
