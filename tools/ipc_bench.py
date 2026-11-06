#!/usr/bin/env python3
"""Synthetic Unix-socket saturation model, without GUI capture or desktop access.

Compares one-send-per-10ms scheduling with writable-event scheduling, using
the same 1 MiB send-buffer request as M2Bridge. Not a display/FPS benchmark.
"""
import argparse
import json
import select
import socket
import threading
import time


def run(mode, frame_bytes, frames):
    writer, reader = socket.socketpair()
    writer.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024 * 1024)
    writer.setblocking(False)
    reader.settimeout(10)
    received = 0
    failures = []

    def drain():
        nonlocal received
        try:
            while True:
                chunk = reader.recv(256 * 1024)
                if not chunk:
                    break
                received += len(chunk)
        except Exception as error:
            failures.append(str(error))
        finally:
            reader.close()

    sink = threading.Thread(target=drain, daemon=True)
    sink.start()
    payload = memoryview(bytes(frame_bytes))
    calls = 0
    start = time.monotonic()
    deadline = start + 15
    try:
        for _ in range(frames):
            offset = 0
            while offset < frame_bytes:
                if time.monotonic() > deadline:
                    raise TimeoutError("transport benchmark exceeded 15 seconds")
                if mode == "timer":
                    time.sleep(0.01)
                elif not select.select([], [writer], [], 1)[1]:
                    continue
                calls += 1
                try:
                    offset += writer.send(payload[offset:])
                except BlockingIOError:
                    continue
        writer.shutdown(socket.SHUT_WR)
        sink.join(timeout=10)
        elapsed = time.monotonic() - start
        if sink.is_alive() or failures or received != frames * frame_bytes:
            raise RuntimeError("incomplete receiver: " + repr(failures))
        return {
            "scheduling": mode,
            "frame_bytes": frame_bytes,
            "frames_received": frames,
            "bytes_received": received,
            "seconds": elapsed,
            "transport_frames_per_second": frames / elapsed,
            "send_calls": calls,
        }
    finally:
        writer.close()
        sink.join(timeout=11)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frames", type=int, default=30)
    args = parser.parse_args()
    if not 1 <= args.frames <= 60:
        parser.error("frames must be between 1 and 60")
    print(json.dumps({
        "frame_size": "1920x1200 BGRA",
        "results": [run(mode, 1920 * 1200 * 4, args.frames)
                    for mode in ("timer", "writable")],
        "limitations": "One synthetic local socket, saturated producer, no capture, relay, "
                       "encoding or client rendering. Transport results are not displayed FPS.",
    }, indent=2))


if __name__ == "__main__":
    main()
