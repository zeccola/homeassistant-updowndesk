#!/usr/bin/env python3
"""Standalone CLI for poking a PairLink desk, for use without Home Assistant.

It shares the integration's protocol module, so anything proved here holds for
the integration too.  Only bleak is required::

    pip install bleak

    python tools/desk_cli.py scan
    python tools/desk_cli.py --address EC:C5:7F:AE:26:7F info
    python tools/desk_cli.py --address ... listen 30
    python tools/desk_cli.py --address ... up 0.5
    python tools/desk_cli.py --address ... goto 100

Movement always ends in a STOP, including on Ctrl+C.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
from pathlib import Path
import sys

from bleak import BleakClient, BleakScanner

# protocol.py has no Home Assistant imports, so it loads on its own.
sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "custom_components" / "updown_desk")
)

from protocol import (
    COUNTS_PER_CM,
    NOTIFY_UUID,
    SERVICE_UUID,
    STAGE_ACKS,
    STAGE_READY,
    WRITE_UUID,
    Calibration,
    Init,
    Op,
    ProtocolError,
    build_frame,
    parse_frame,
    truncate_cm,
)

MAX_HOLD = 3.0


class DeskCLI:
    """A minimal client: send frames, print everything that comes back."""

    def __init__(self, client: BleakClient, *, follow: bool = True) -> None:
        """Wrap a connected bleak client."""
        self.client = client
        self.follow = follow
        self.queue: asyncio.Queue = asyncio.Queue()
        self.calibration: Calibration | None = None
        self.run_hall: int | None = None
        self._write_response = True
        self._tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        """Subscribe to notifications and work out how to write."""
        write_char = self.client.services.get_characteristic(WRITE_UUID)
        notify_char = self.client.services.get_characteristic(NOTIFY_UUID)
        if write_char is None or notify_char is None:
            sys.exit("!! ff01/ff02 not found. Run `services` to dump the GATT layout.")
        self._write_response = "write-without-response" not in write_char.properties
        await self.client.start_notify(notify_char, self._on_notify)

    def _on_notify(self, _char, data: bytearray) -> None:
        raw = bytes(data)
        try:
            frame = parse_frame(raw)
        except ProtocolError as err:
            print(f"  <- {raw.hex(' ')}   unparseable: {err}")
            return

        crc = "" if frame.crc_valid else "  [CRC MISMATCH]"
        print(f"  <- {raw.hex(' ')}   {frame}{crc}")

        if frame.op in (Op.QUERY, Op.STOP) and not frame.is_error:
            self.run_hall = frame.value
            if self.calibration is not None:
                print(
                    f"     = {truncate_cm(self.calibration.hall_to_cm(frame.value)):.1f} cm"
                )
        if frame.op == Op.STAGE and not frame.is_error:
            self._handle_stage(frame.value)
        self.queue.put_nowait(frame)

    def _handle_stage(self, stage: int) -> None:
        if (ack := STAGE_ACKS.get(stage)) is not None:
            self._spawn(self.send(Op.STAGE, 1, ack))
        elif stage == STAGE_READY and self.follow:
            self._spawn(self.send(Op.QUERY))

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def send(self, op: int, p1: int = 1, data: int = 0) -> None:
        """Write one frame."""
        frame = build_frame(op, p1, data)
        name = Op(op).name.lower() if op in [item.value for item in Op] else op
        print(f"  -> {frame.hex(' ')}   op={name} p1={p1} data={data}")
        await self.client.write_gatt_char(WRITE_UUID, frame, response=self._write_response)

    async def request(
        self, op: int, p1: int = 1, expect_p1: int | None = None, timeout: float = 2.0
    ) -> int | None:
        """Send a command and return the value from its reply."""
        for _ in range(3):
            await self.send(op, p1)
            loop = asyncio.get_running_loop()
            end = loop.time() + timeout
            while (left := end - loop.time()) > 0:
                try:
                    frame = await asyncio.wait_for(self.queue.get(), left)
                except TimeoutError:
                    break
                # Replies match on the opcode: a p1=1 query answers with p1=6.
                if (
                    frame.op == op
                    and not frame.is_error
                    and (expect_p1 is None or frame.p1 == expect_p1)
                ):
                    return frame.value
        return None

    async def read_calibration(self) -> Calibration | None:
        """Walk the INIT registers one at a time."""
        values: dict[int, int] = {}
        for p1 in range(1, 10):
            values[p1] = await self.request(Op.INIT, p1, expect_p1=p1)
            await asyncio.sleep(0.05)
        print(f"\ninit registers: {values}")

        needed = (Init.BASE_HALL, Init.MIN_HALL, Init.MAX_HALL, Init.MODEL)
        if any(values.get(key) is None for key in needed):
            print("!! incomplete calibration")
            return None

        calibration = Calibration(
            base_hall=values[Init.BASE_HALL],
            min_hall=values[Init.MIN_HALL],
            max_hall=values[Init.MAX_HALL],
            model=values[Init.MODEL],
        )
        self.calibration = calibration
        self.run_hall = values.get(Init.RUN_HALL)

        print(
            f"base={calibration.base_hall} min={calibration.min_hall} "
            f"max={calibration.max_hall} model={calibration.model} "
            f"run_hall={self.run_hall}"
        )
        if (calibration.model & 0xFF) not in COUNTS_PER_CM:
            print(f"!! unknown model index {calibration.model}: cannot convert to cm")
            return calibration
        print(f"counts/cm = {calibration.counts_per_cm}")
        print(f"range     = {calibration.min_cm:.1f} .. {calibration.max_cm:.1f} cm")
        if self.run_hall is not None:
            print(
                f"height    = {truncate_cm(calibration.hall_to_cm(self.run_hall)):.1f} cm"
            )
        return calibration

    async def hold(self, op: Op, seconds: float) -> None:
        """Hold a direction, then always stop."""
        try:
            await self.send(op)
            await asyncio.sleep(min(seconds, MAX_HOLD))
        finally:
            await self.stop()

    async def stop(self) -> None:
        """Stop the motors and report the final position."""
        with contextlib.suppress(Exception):
            await self.request(Op.STOP)

    async def goto(self, target_cm: float) -> None:
        """Run a closed loop move, mirroring what the integration does."""
        if self.calibration is None and await self.read_calibration() is None:
            return
        calibration = self.calibration
        target = calibration.cm_to_hall(target_cm)
        current = await self.request(Op.QUERY)
        if current is None:
            print("!! no height reply; aborting")
            return
        print(f"\nmoving from {current} to {target} ({target_cm:.1f} cm)")
        if abs(target - current) <= 4:
            print("already there")
            return

        op = Op.UP if target > current else Op.DOWN
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 60
        last, last_progress = current, loop.time()
        try:
            await self.send(op)
            while loop.time() < deadline:
                await asyncio.sleep(0.15)
                hall = await self.request(Op.QUERY, timeout=1.0)
                if hall is None:
                    continue
                if (op is Op.UP and hall >= target - 6) or (
                    op is Op.DOWN and hall <= target + 6
                ):
                    break
                if abs(hall - last) >= 2:
                    last, last_progress = hall, loop.time()
                elif loop.time() - last_progress > 3:
                    print("!! no progress; stopping")
                    break
        finally:
            await self.stop()


async def cmd_scan() -> None:
    """List devices advertising the PairLink service."""
    print("Scanning 10 s for PairLink controllers...")
    devices = await BleakScanner.discover(timeout=10, return_adv=True)
    found = False
    for device, adv in devices.values():
        uuids = [u.lower() for u in (adv.service_uuids or [])]
        if SERVICE_UUID in uuids:
            found = True
            print(f"  {device.address}  {device.name or '(unnamed)'}  rssi={adv.rssi}")
    if not found:
        print("  none found. Is the phone app still connected?")


async def run(args: argparse.Namespace) -> None:
    """Connect and dispatch the requested command."""
    if args.cmd == "scan":
        await cmd_scan()
        return

    if not args.address:
        sys.exit("--address is required (run `scan` first)")

    print(f"Looking for {args.address} (close the phone app first)...")
    device = await BleakScanner.find_device_by_address(args.address, timeout=15)
    if device is None:
        sys.exit("Not found. Is the phone app connected, or Bluetooth off?")

    async with BleakClient(device) as client:
        print(f"Connected: {device.name or device.address}")

        if args.cmd == "services":
            for service in client.services:
                print(f"[service] {service.uuid}  {service.description}")
                for char in service.characteristics:
                    print(f"    {char.uuid}  {','.join(char.properties)}")
            return

        desk = DeskCLI(client, follow=not args.no_follow)
        await desk.start()
        await asyncio.sleep(0.4)

        try:
            if args.cmd == "listen":
                seconds = float(args.arg[0]) if args.arg else 20
                print(f"Listening {seconds:.0f} s. Try the handset now.")
                await asyncio.sleep(seconds)
            elif args.cmd == "info":
                await desk.read_calibration()
            elif args.cmd == "height":
                value = await desk.request(Op.QUERY)
                print(f"run_hall = {value}" if value is not None else "no reply")
            elif args.cmd in ("up", "down"):
                seconds = float(args.arg[0]) if args.arg else 0.5
                await desk.hold(Op.UP if args.cmd == "up" else Op.DOWN, seconds)
            elif args.cmd == "stop":
                await desk.stop()
            elif args.cmd in ("sit", "middle", "stand"):
                await desk.read_calibration()
                op = {
                    "sit": Op.PRESET_SIT,
                    "middle": Op.PRESET_MIDDLE,
                    "stand": Op.PRESET_STAND,
                }
                await desk.send(op[args.cmd])
                await asyncio.sleep(float(args.arg[0]) if args.arg else 20)
                await desk.stop()
            elif args.cmd == "goto":
                if not args.arg:
                    sys.exit("goto needs a height in cm")
                await desk.goto(float(args.arg[0]))
            elif args.cmd == "raw":
                op, p1, data = (int(x, 0) for x in [*args.arg, "1", "0"][:3])
                await desk.send(op, p1, data)
                await asyncio.sleep(3)
        except (KeyboardInterrupt, asyncio.CancelledError):
            await desk.stop()
            raise
        finally:
            await asyncio.sleep(0.2)


def main() -> None:
    """Parse arguments and run."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--address", help="the desk's Bluetooth address")
    parser.add_argument(
        "--no-follow",
        action="store_true",
        help="do not answer the idle handshake with a height query",
    )
    parser.add_argument(
        "cmd",
        choices=[
            "scan",
            "services",
            "listen",
            "height",
            "info",
            "up",
            "down",
            "stop",
            "sit",
            "middle",
            "stand",
            "goto",
            "raw",
        ],
    )
    parser.add_argument("arg", nargs="*")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
