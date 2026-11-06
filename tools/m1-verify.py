#!/usr/bin/env python3
"""Verify the M1 capture gate from pixels decoded by the two GUI agents.

PASS CAPTURE is deliberately not PASS M1: desktop capture and session-routed
input (including another app) still need their own recorded experiments.
"""
import argparse
from collections import Counter
import json
import sys
from pathlib import Path


class Problem(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise Problem(message)


class Log:
    def __init__(self, path):
        self.rows = [json.loads(line) for line in Path(path).read_text().splitlines() if line]
        require(self.rows, f"{path}: empty log")
        self.first = self.rows[0]
        require(self.first.get("kind") == "start", "missing start record")
        self.uid = self.first["uid"]
        self.nonce = self.first["nonce"]
        require(type(self.uid) is int and self.uid > 0, "invalid UID")
        require(type(self.nonce) is int and 0 < self.nonce <= 0xffffffff, "invalid nonce")
        require(self.first["capture_mode"] in ("window", "desktop"), "invalid capture mode")
        require(self.first["audit_session_id"] > 0, "invalid audit session")
        self.heartbeats = []
        previous = -1
        identity = ("uid", "audit_session_id", "run_id", "boot_id", "os_build", "nonce", "capture_mode")
        for row in self.rows:
            require(row["schema"] == 1, "unsupported schema")
            require(all(row[k] == self.first[k] for k in identity), "session/run identity changed")
            require(row["gui_uid"] == self.uid and row["login_complete"] is True, "invalid GUI identity")
            require(type(row["uptime_ms"]) is int and row["uptime_ms"] >= previous, "time moved backwards")
            previous = row["uptime_ms"]
            require(row["kind"] not in ("error", "capture_error"), f"agent error: {row.get('message')}")
            if row["kind"] == "frame" and row["decoded"]:
                require(row["status"] == 0, "decoded non-complete frame")
                require(row["marker_uid"] == self.uid and row["marker_nonce"] == self.nonce,
                        "captured another user's or stale run's pixels")
                require(0 <= row["marker_tick"] <= row["drawn_tick"], "invalid captured counter")
            if row["kind"] == "heartbeat":
                if self.heartbeats:
                    require(row["uptime_ms"] > self.heartbeats[-1]["uptime_ms"], "duplicate heartbeat time")
                self.heartbeats.append(row)
        require(self.rows[-1]["kind"] == "stop", "missing clean stop record")
        require(self.heartbeats, "no heartbeats")

    def spans(self, console):
        spans = []
        for index, row in enumerate(self.heartbeats):
            if row["console_uid"] != console:
                continue
            # A console transition or a sampling gap starts a fresh interval.
            prior = self.heartbeats[index - 1] if index else None
            t = row["uptime_ms"]
            if prior and prior["console_uid"] == console and t - prior["uptime_ms"] <= 2500:
                spans[-1][1] = t
            else:
                spans.append([t, t])
        return spans

    def verify_interval(self, start, end):
        rows = [r for r in self.heartbeats if start <= r["uptime_ms"] <= end]
        require(len(rows) >= 10, "not enough heartbeats in stable interval")
        for row in rows:
            require(type(row["change_age_ms"]) is int and 0 <= row["change_age_ms"] <= 3500,
                    f"uid={self.uid}: captured picture stopped updating at {row['uptime_ms']}")
            require(type(row["frame_age_ms"]) is int and 0 <= row["frame_age_ms"] <= 3500,
                    f"uid={self.uid}: capture stopped delivering frames")
        frames = [r for r in self.rows if r["kind"] == "frame" and r["decoded"]
                  and start - 2000 <= r["uptime_ms"] <= end]
        ticks = [r["marker_tick"] for r in frames]
        require(len(set(ticks)) >= 10, f"uid={self.uid}: not enough fresh pixel counters")
        require(all(b >= a for a, b in zip(ticks, ticks[1:])), "pixel counter moved backwards")
        require(ticks[-1] - ticks[0] >= 10, "pixel counter did not advance")


def verify_capture(a, b):
    require(a.uid != b.uid and a.first["audit_session_id"] != b.first["audit_session_id"], "distinct users and audit sessions required")
    require(a.nonce != b.nonce, "distinct visual nonces required")
    require(all(a.first[k] == b.first[k] for k in ("run_id", "boot_id", "os_build", "capture_mode")), "logs must share run, boot, OS and capture mode")
    for console in (a.uid, b.uid):
        checked = []
        for left in a.spans(console):
            for right in b.spans(console):
                start, end = max(left[0], right[0]) + 5000, min(left[1], right[1])
                if end - start >= 15000:
                    a.verify_interval(start, end)
                    b.verify_interval(start, end)
                    checked.append(end - start)
        require(checked, f"need 20 s of simultaneous observations with uid={console} on console")
        print(f"console_uid={console}: fresh own pixels in both agents for {max(checked)} ms after transition grace")
    print(f"PASS CAPTURE ({a.first['capture_mode']}): {a.first['run_id']}; this is not full M1 acceptance")


def verify_input(a, b):
    """One simultaneous batch, with a stable physical console, per log pair."""
    require(a.uid != b.uid and a.nonce != b.nonce, "distinct input identities required")
    require(all(a.first[k] == b.first[k] for k in ("run_id", "boot_id", "os_build")), "input logs must share run and boot")
    intervals = []
    consoles = set()
    for own, other in ((a, b), (b, a)):
        kinds = Counter(r["kind"] for r in own.rows)
        require(kinds["input_started"] == 1 and kinds["input_finished"] == 1,
                f"uid={own.uid}: one complete input batch required")
        require(not (kinds["input_aborted"] or kinds["input_rejected"]), "input aborted or rejected")
        posts = [r for r in own.rows if r["kind"] == "input_post"]
        received = [r for r in own.rows if r["kind"] == "input_received" and r["tag"] != "0000000000000000"]
        expected = {f"{own.nonce:08x}{trial * 256 + index:08x}": (trial, event_type)
                    for trial in range(1, 101) for index, event_type in enumerate((10, 11, 1, 6, 2), 1)}
        require(Counter(r["tag"] for r in posts) == Counter(expected.keys()), "missing or duplicate input posts")
        require(Counter(r["tag"] for r in received) == Counter(expected.keys()), "missing, duplicate or foreign input receipts")
        require(not any(r["kind"] == "input_received" and r["tag"] in expected for r in other.rows), "input crossed to the other account")
        posted = {r["tag"]: r for r in posts}
        for row in posts:
            trial, event_type = expected[row["tag"]]
            require(row["trial"] == trial and row["event_type"] == event_type
                    and row["route"] == "kCGSessionEventTap", "wrong input type or process-directed route")
        for row in received:
            trial, event_type = expected[row["tag"]]
            require(row["event_type"] == event_type, "received wrong event type")
            require(0 <= row["uptime_ms"] - posted[row["tag"]]["uptime_ms"] <= 2000, "input delivery missing or delayed")
            # Unicode text is delivered by key-down. AppKit may translate
            # key-up from the virtual keycode (0 in this Unicode-only probe)
            # rather than retaining CGEventKeyboardSetUnicodeString's value.
            # Key-up still needs its exact tag, type, UID and delivery time.
            if event_type == 10:
                require(row["text"] == chr(ord("a") + trial % 26), "received wrong keyboard text")
            elif event_type in (1, 6, 2):
                # Probe coordinates in the receiving window's bottom-up space.
                x, y = 100 + trial % 20 * 10, 100
                if event_type != 1:
                    x, y = x + 12, y - 8
                require(abs(row["x"] - x) <= 1 and abs(row["y"] - y) <= 1,
                        "received wrong mouse coordinates")
        start, end = posts[0]["uptime_ms"], received[-1]["uptime_ms"]
        intervals.append((start, end))
        observations = [r for r in own.heartbeats if start <= r["uptime_ms"] <= end]
        require(len(observations) >= 15, "input batch needs continuous heartbeat observations")
        require(all(y["uptime_ms"] - x["uptime_ms"] <= 2500 for x, y in zip(observations, observations[1:])), "input heartbeat gap")
        consoles.update(r["console_uid"] for r in observations + posts + received)
        print(f"uid={own.uid}: all 500 session-routed input events received exactly once")
    require(len(consoles) == 1 and next(iter(consoles)) in (a.uid, b.uid), "input changed or lost console identity")
    require(min(i[1] for i in intervals) - max(i[0] for i in intervals) >= 15000, "input batches did not run simultaneously for 15 s")
    print(f"PASS INPUT BATCH: console_uid={next(iter(consoles))}; reverse console direction and cross-app tests still required")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("a")
    parser.add_argument("b")
    parser.add_argument("--input", action="store_true", help="also verify one concurrent input batch")
    args = parser.parse_args()
    phase = "CAPTURE"
    try:
        a, b = Log(args.a), Log(args.b)
        verify_capture(a, b)
        if args.input:
            phase = "INPUT"
            verify_input(a, b)
    except (Problem, KeyError, TypeError, OSError, json.JSONDecodeError) as error:
        print(f"FAIL {phase}: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
