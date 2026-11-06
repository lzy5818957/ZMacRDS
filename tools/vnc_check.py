#!/usr/bin/env python3
"""Bounded, read-only Apple VNC test through an existing loopback SSH tunnel.

Only synthetic marker identity/ticks are emitted. Frames stay in memory;
clipboard and input are never requested. Requires vnc-test-requirements.txt.
"""
import argparse
import asyncio
import binascii
import hashlib
import json
import os
import re
import stat
import struct
import time
from pathlib import Path


def read_config(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        meta = os.fstat(fd)
        if not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid() or meta.st_mode & 0o077:
            raise ValueError("configuration must be a private, owned regular file")
        raw = os.read(fd, 65537)
        if len(raw) > 65536:
            raise ValueError("configuration too large")
        cfg = json.loads(raw)
    finally:
        os.close(fd)
    required = {"port", "username", "password", "uid", "nonce", "marker", "seconds", "host_key_sha256"}
    if set(cfg) != required or not 1024 <= cfg["port"] <= 65535:
        raise ValueError("invalid configuration keys or port")
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", cfg["username"]):
        raise ValueError("invalid username")
    if not 1 <= cfg["seconds"] <= 3600 or not re.fullmatch(r"[0-9a-f]{64}", cfg["host_key_sha256"]):
        raise ValueError("invalid bounds or host key pin")
    if any(type(cfg[k]) is not int or not 0 < cfg[k] < 2**32 for k in ("uid", "nonce")):
        raise ValueError("invalid marker identity")
    if not isinstance(cfg["password"], str) or not cfg["password"]:
        raise ValueError("missing password")
    if len(cfg["marker"]) != 2 or any(type(n) is not int or n < 0 for n in cfg["marker"]):
        raise ValueError("invalid marker coordinates")
    return cfg


def marker(frame, xy):
    x, y = xy
    if y >= frame.shape[0] or x + 524 > frame.shape[1]:
        return None
    row = frame[y, x:x + 524:4, :3]
    magenta = lambda p: int(p[0]) > 180 and int(p[1]) < 80 and int(p[2]) > 180
    if not (magenta(row[0]) and magenta(row[2]) and
            int(row[1][0]) < 80 and int(row[1][1]) > 180 and int(row[1][2]) > 180):
        return None
    bits = []
    for pixel in row[3:]:
        if min(pixel) > 180:
            bits.append(1)
        elif max(pixel) < 80:
            bits.append(0)
        else:
            return None
    raw = bytes(sum(bits[i * 8 + j] << (7 - j) for j in range(8)) for i in range(16))
    if raw[:2] != b"\xa5\x3c" or binascii.crc_hqx(raw[:14], 65535) != int.from_bytes(raw[14:], "big"):
        return None
    return struct.unpack(">III", raw[2:14])


async def connect(cfg, shared=True, decoder="zlib"):
    from vnc_gateway import pinned_connection
    return pinned_connection(cfg["port"], cfg["username"], cfg["password"],
                             cfg["host_key_sha256"], shared, decoder)


async def observe(cfg):
    started = time.monotonic()
    async with await connect(cfg) as client:
        count, first, last = 0, None, None
        refreshes = 0
        progress = time.monotonic()
        changed = time.monotonic()
        # Request a full initial screen once, then incremental updates. A
        # repeated screenshot() would request full pixels every iteration.
        await asyncio.wait_for(client.screenshot(), 8)
        while time.monotonic() - started < cfg["seconds"]:
            found = marker(client.video.as_rgba(), cfg["marker"])
            if found:
                if found[:2] != (cfg["uid"], cfg["nonce"]):
                    raise ValueError("foreign desktop marker")
                if last is not None and found[2] < last:
                    raise ValueError("marker tick reversed")
                if found[2] != last:
                    count += 1
                    first = found[2] if first is None else first
                    last = found[2]
                    changed = time.monotonic()
            if time.monotonic() - changed > 6:
                raise ValueError("no fresh expected desktop marker")
            client.video.refresh()
            read = asyncio.create_task(client.read())
            try:
                done, _ = await asyncio.wait({read}, timeout=2)
                if not done:
                    # Keep the same partially decoded message alive. Cancelling
                    # read() and restarting at a message boundary corrupts RFB.
                    client.writer.write(b"\x03\0" + struct.pack(">HHHH", 0, 0, client.video.width, client.video.height))
                    await client.drain()
                    refreshes += 1
                    done, _ = await asyncio.wait({read}, timeout=4)
                    if not done:
                        raise TimeoutError("no framebuffer response after full refresh")
                await read
            except BaseException as error:
                read.cancel()
                await asyncio.gather(read, return_exceptions=True)
                print(json.dumps({"event":"vnc_observation_failed","uid":cfg["uid"],
                    "error_type":type(error).__name__,"seconds":time.monotonic()-started,
                    "last_tick":last,"full_refreshes":refreshes}),flush=True)
                raise
            if time.monotonic()-progress>=60:
                print(json.dumps({"event":"vnc_progress","uid":cfg["uid"],
                    "seconds":time.monotonic()-started,"distinct_observed_ticks":count,
                    "last_tick":last,"full_refreshes":refreshes}),flush=True)
                progress=time.monotonic()
        if count < 2:
            raise ValueError("insufficient changing pixels")
        return {"event": "vnc_observed", "uid": cfg["uid"], "seconds": time.monotonic() - started,
                "distinct_observed_ticks": count, "first_tick": first, "last_tick": last,
                "full_refreshes": refreshes,
                "read_only": True, "result": "PASS"}


async def main(args):
    configs = [read_config(p) for p in args.configs]
    results = await asyncio.gather(*(observe(c) for c in configs))
    for result in results:
        print(json.dumps(result), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("configs", nargs="+")
    args = parser.parse_args()
    try:
        asyncio.run(main(args))
    except Exception as error:
        print(json.dumps({"result": "FAIL", "error_type":type(error).__name__, "error": str(error)}), flush=True)
        raise SystemExit(1)
