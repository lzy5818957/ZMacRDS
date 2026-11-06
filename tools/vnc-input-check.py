#!/usr/bin/env python3
"""Bounded input-to-decoded-pixel check in the project's synthetic editor only.

Requires its UID/nonce/length marker at the configured coordinates before
sending any input. No clipboard, screenshots on disk, or arbitrary workload.
Run after the read-only observation: a new login takes over that account.
"""
import argparse
import asyncio
import json
import math
import struct
import time

from vnc_check import connect, marker, read_config


async def run(cfg, zrle=False, details=False):
    async with await connect(cfg, decoder="zrle" if zrle else "zlib") as client:
        await asyncio.wait_for(client.screenshot(), 8)
        initial = marker(client.video.as_rgba(), cfg["marker"])
        if not initial or initial[:2] != (cfg["uid"], cfg["nonce"]):
            raise ValueError("expected synthetic editor is not visible; no input sent")
        # Coordinates are relative to the verified test editor header.
        client.mouse.move(cfg["marker"][0] + 40, cfg["marker"][1] + 80)
        client.mouse.click()
        await client.drain()
        await asyncio.sleep(.2)  # AppKit focus transition, outside measured interval.
        samples = []
        updates = asyncio.Queue(maxsize=1)

        def publish(value):
            if updates.full():
                updates.get_nowait()
            updates.put_nowait(value)

        async def receive():
            try:
                while True:
                    # A GUI viewer keeps reading/requesting while the person
                    # pauses between keys. Stopping the reader between samples
                    # instead measures the native server's idle wake-up path.
                    client.video.refresh()
                    await client.drain()
                    await client.read()
                    observed = marker(client.video.as_rgba(), cfg["marker"])
                    if observed:
                        if observed[:2] != initial[:2]:
                            raise ValueError("foreign editor identity")
                        publish((observed[2], time.monotonic()))
            except Exception as error:
                publish(error)

        reader = asyncio.create_task(receive())
        tick = initial[2]
        try:
            for text in ["x"] * 30 + ["中", "文", "恢", "复"]:
                started = time.monotonic()
                key = ord(text) if ord(text) < 256 else 0x01000000 | ord(text)
                for down in (1, 0):
                    client.writer.write(struct.pack(">BBHI", 4, down, 0, key))
                await client.drain()
                tick += 1
                async with asyncio.timeout(3):
                    while True:
                        observed = await updates.get()
                        if isinstance(observed, Exception):
                            raise observed
                        length, visible = observed
                        if length > tick:
                            raise ValueError("unexpected document change")
                        if length == tick:
                            break
                samples.append((visible - started) * 1000)
                if details:
                    print(json.dumps({"event":"input_sample","uid":cfg["uid"],"tick":tick,
                                      "sent_monotonic":started,"visible_monotonic":visible}),flush=True)
                await asyncio.sleep(.15)
        finally:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        ordered = sorted(samples)
        return {"event": "vnc_input_visible", "uid": cfg["uid"], "result": "PASS",
                "samples": len(samples), "p50_ms": ordered[math.ceil(len(samples)*.5)-1],
                "p95_ms": ordered[math.ceil(len(samples)*.95)-1], "max_ms": max(samples),
                "unicode_keysyms": 4, "final_length": tick,
                "scope": "input write to CRC-checked decoded document marker; continuous incremental reader",
                "decoder": "asyncvnc2 ZRLE-preferred" if zrle else "asyncvnc zlib"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config")
    parser.add_argument("--zrle", action="store_true", help="use asyncvnc2's ZRLE-preferred decoder")
    parser.add_argument("--details", action="store_true", help="emit synthetic timing samples")
    args = parser.parse_args()
    try:
        cfg = read_config(args.config)
        print(json.dumps(asyncio.run(asyncio.wait_for(run(cfg, args.zrle, args.details), cfg["seconds"]))), flush=True)
    except Exception as error:
        print(json.dumps({"result": "FAIL", "error_type": type(error).__name__,
                          "error": str(error)}), flush=True)
        raise SystemExit(1)
