"""Capture evidence must reject frozen, cross-user and incomplete streams."""
import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("m1_verify", ROOT / "tools/m1-verify.py")
VERIFY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFY)


def rows(uid):
    result = []
    for i in range(65):
        base = dict(schema=1, uid=uid, gui_uid=uid, login_complete=True, nonce=100 + uid,
                    audit_session_id=1000 + uid, run_id="fixture", boot_id="boot", os_build="build",
                    capture_mode="window", uptime_ms=i * 1000, console_uid=502 if i < 32 else 503)
        if i == 0:
            result.append(dict(base, kind="start"))
        result.append(dict(base, kind="frame", decoded=True, status=0, marker_uid=uid,
                           marker_nonce=100 + uid, marker_tick=i, drawn_tick=i))
        result.append(dict(base, kind="heartbeat", captured_tick=i, drawn_tick=i, change_age_ms=20, frame_age_ms=20))
    result.append(dict(result[-1], kind="stop", uptime_ms=65000))
    return result


class CaptureTests(unittest.TestCase):
    def verify(self, a, b):
        with tempfile.TemporaryDirectory() as folder:
            paths = [Path(folder) / side for side in ("A", "B")]
            for path, records in zip(paths, (a, b)):
                path.write_text("".join(json.dumps(r) + "\n" for r in records))
            VERIFY.verify_capture(*(VERIFY.Log(p) for p in paths))

    def test_both_foreground_and_background_pixels_advance(self):
        self.verify(rows(502), rows(503))

    def test_recovered_display_change_only_with_fresh_pixels_after_grace(self):
        def interrupted(until):
            result = []
            for row in rows(503):
                tick = row["uptime_ms"] // 1000
                if row["kind"] == "frame" and 32 <= tick <= until:
                    continue
                if row["kind"] == "heartbeat" and 32 <= tick <= until:
                    row = dict(row, captured_tick=31, change_age_ms=(tick-31)*1000,
                               frame_age_ms=(tick-31)*1000)
                result.append(row)
            result.append(dict(result[0], kind="capture_interrupted", uptime_ms=32000,
                               console_uid=503, code=-3815, recovery_attempt=1))
            return sorted(result, key=lambda r: r["uptime_ms"])
        self.verify(rows(502), interrupted(34))
        with self.assertRaises(VERIFY.Problem):
            self.verify(rows(502), interrupted(50))

    def test_rejects_false_positive_capture_evidence(self):
        cases = {
            "frozen": [dict(r, change_age_ms=10000) if r["kind"] == "heartbeat" else r for r in rows(503)],
            "wrong pixels": [dict(r, marker_uid=502) if r["kind"] == "frame" else r for r in rows(503)],
            "stale run pixels": [dict(r, marker_nonce=77) if r["kind"] == "frame" else r for r in rows(503)],
            "invented freshness": [dict(r, marker_tick=0) if r["kind"] == "frame" else r for r in rows(503)],
            "no frames": [r for r in rows(503) if r["kind"] != "frame"],
            "only foreground": [dict(r, console_uid=503) for r in rows(503)],
            "session changed": rows(503)[:-1] + [dict(rows(503)[-1], audit_session_id=9)],
            "truncated": rows(503)[:-1],
            "capture failed": rows(503)[:-1] + [dict(rows(503)[-1], kind="capture_error", message="suspended")],
        }
        for name, records in cases.items():
            with self.subTest(name=name), self.assertRaises(VERIFY.Problem):
                self.verify(rows(502), records)

    def test_marker_decodes_pixels_and_rejects_corruption(self):
        source = r'''
        #include "m1-marker.h"
        #include <assert.h>
        #include <stdlib.h>
        int main(void) {
            enum { W=640, H=480, STRIDE=W*4+32 };
            uint8_t *pixels=calloc(H,STRIDE), bytes[16];
            M1Marker original={502,0xfedcba98,0x98765432}, decoded={0};
            m1Encode(original,bytes);
            for (int y=104;y<128;y++) for (int c=0;c<M1_CELLS;c++) for (int dx=0;dx<M1_CELL;dx++) {
                uint8_t *p=pixels+y*STRIDE+(24+c*M1_CELL+dx)*4;
                if(c<3) {p[0]=255;p[1]=c==1?255:0;p[2]=c==1?0:255;}
                else {int bit=c-3; p[0]=p[1]=p[2]=(bytes[bit/8]&(1<<(7-bit%8)))?255:0;}
                p[3]=255;
            }
            assert(m1Decode(pixels,W,H,STRIDE,&decoded));
            assert(decoded.uid==original.uid && decoded.nonce==original.nonce && decoded.tick==original.tick);
            for (int y=104;y<128;y++) for(int dx=0;dx<M1_CELL;dx++) {
                uint8_t *p=pixels+y*STRIDE+(24+50*M1_CELL+dx)*4;
                p[0]=255-p[0];p[1]=255-p[1];p[2]=255-p[2];
            }
            assert(!m1Decode(pixels,W,H,STRIDE,&decoded));
            assert(!m1Decode(pixels,100,H,STRIDE,&decoded));
            assert(!m1Decode(pixels,W,H,1,&decoded));
            free(pixels);
        }
        '''
        with tempfile.TemporaryDirectory() as folder:
            cfile, binary = Path(folder) / "marker.c", Path(folder) / "marker"
            cfile.write_text(source)
            subprocess.run(["xcrun", "clang", "-Wall", "-Wextra", "-Werror", "-fsanitize=address,undefined",
                            "-I", str(ROOT / "tools"), str(cfile), "-o", str(binary)], check=True)
            subprocess.run([binary], check=True)


class InputTests(unittest.TestCase):
    def logs(self):
        logs = []
        for uid in (502, 503):
            log = type("InputLog", (), {})()
            log.uid, log.nonce = uid, uid + 100
            log.first = dict(run_id="input", boot_id="boot", os_build="build")
            log.heartbeats = [dict(uptime_ms=i * 1000, console_uid=502) for i in range(21)]
            log.rows = [dict(kind="input_started")]
            for trial in range(1, 101):
                for index, event_type in enumerate((10, 11, 1, 6, 2), 1):
                    base = dict(tag=f"{log.nonce:08x}{trial * 256 + index:08x}", event_type=event_type,
                                uptime_ms=trial * 200, console_uid=502)
                    log.rows.append(dict(base, kind="input_post", trial=trial, route="kCGSessionEventTap"))
                    # Actual AppKit key-up uses virtual keycode 0 -> 'a';
                    # the Unicode override supplies the text on key-down.
                    log.rows.append(dict(base, kind="input_received",
                                         text="a" if event_type == 11 else chr(ord("a") + trial % 26),
                                         x=100 + trial % 20 * 10 + (12 if event_type in (6, 2) else 0),
                                         y=92 if event_type in (6, 2) else 100))
            log.rows.append(dict(kind="input_finished"))
            logs.append(log)
        return logs

    def test_exactly_once_session_input(self):
        VERIFY.verify_input(*self.logs())

    def test_rejects_misdelivery_duplicates_focus_switch_and_pid_routing(self):
        for fault in ("missing", "duplicate", "cross-user", "console", "pid", "text", "mouse"):
            a, b = self.logs()
            receipt = next(r for r in b.rows if r["kind"] == "input_received")
            if fault == "missing": b.rows.remove(receipt)
            elif fault == "duplicate": b.rows.append(dict(receipt))
            elif fault == "cross-user": a.rows.append(dict(receipt))
            elif fault == "console": b.heartbeats[10]["console_uid"] = 503
            elif fault == "pid": next(r for r in b.rows if r["kind"] == "input_post")["route"] = "pid"
            elif fault == "text": receipt["text"] = "wrong"
            elif fault == "mouse":
                next(r for r in b.rows if r["kind"] == "input_received" and r["event_type"] == 6)["x"] += 10
            with self.subTest(fault=fault), self.assertRaises(VERIFY.Problem):
                VERIFY.verify_input(a, b)


if __name__ == "__main__":
    unittest.main()
