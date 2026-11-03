"""Synthetic log pairs exercise M0 verdicts without switching the developer's desktop."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

VERIFY = Path(__file__).resolve().parents[1] / "build" / "m0-verify"
BOOT = "14a301ac-658b-4763-9269-591b41545595"


def samples(uid, offset=0):
    rows = []
    for tick in range(10):
        console = 501 if tick < 5 else 502
        rows.append(dict(
            source="m0-launchagent", run_id="fixture", os_build="fixture",
            boot_id=BOOT, uid=uid, gui_uid=uid, audit_session_id=1000 + uid,
            uptime_ms=tick * 2000 + offset, has_gui_session=True,
            login_complete=True, console_uid=console, on_console=uid == console,
            screen_capture_preflight=False, accessibility_trusted=False,
        ))
    return rows


class VerifyTests(unittest.TestCase):
    def verdict(self, a, b, expected, message):
        with tempfile.TemporaryDirectory() as folder:
            files = [Path(folder) / name for name in ("A.jsonl", "B.jsonl")]
            for path, rows in zip(files, (a, b)):
                path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            result = subprocess.run([VERIFY, *files], capture_output=True, text=True)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        self.assertIn(message, result.stdout + result.stderr)

    def test_alternating_console_with_offset_samples_and_denied_permissions(self):
        self.verdict(samples(501), samples(502, 800), 0, "PASS M0")

    def test_missing_quartz_flag_uses_positive_console_identity(self):
        self.verdict(samples(501), [dict(r, on_console=None) for r in samples(502)], 0, "PASS M0")

    def test_inputs_that_must_not_pass(self):
        a, b = samples(501), samples(502, 800)
        cases = [
            (a, [], "empty log"),
            (a, a, "distinct UIDs"),
            (a, [dict(r, audit_session_id=a[0]["audit_session_id"]) for r in b], "distinct audit sessions"),
            (a, [dict(r, boot_id="8728b858-967f-4593-ad87-6cd364f424ce") for r in b], "same run and macOS boot"),
            (a, [dict(r, run_id="stale") for r in b], "same run and macOS boot"),
            (a, b[:-1] + [dict(b[-1], gui_uid=501)], "matching process/GUI UIDs"),
            (a, b[:-1] + [dict(b[-1], login_complete=False)], "logged-in agent"),
            (a, b[:-1] + [dict(b[-1], audit_session_id=4000)], "session, run or boot changed"),
            (a, b + [b[-1]], "time did not advance"),
            (a, [dict(r, uptime_ms=r["uptime_ms"] + 60_000) for r in b], "with A on console"),
            (a[:5], b[:5], "with B on console"),
            (a, [dict(r, on_console=True) for r in b], "with A on console"),
            (a, [dict(r, console_uid=None) for r in b], "with A on console"),
            (a, [b[0], b[4], *b[5:]], "with A on console"),
            (a, [{"uid": 502}], "invalid M0 sample"),
        ]
        for left, right, diagnostic in cases:
            with self.subTest(diagnostic=diagnostic):
                self.verdict(left, right, 1, diagnostic)


if __name__ == "__main__":
    unittest.main()
