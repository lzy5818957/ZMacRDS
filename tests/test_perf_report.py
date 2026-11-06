import importlib.util
import pathlib
import unittest

spec = importlib.util.spec_from_file_location(
    "perf_report", pathlib.Path(__file__).resolve().parents[1] / "tools/perf_report.py")
perf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(perf)


class PerfReportTests(unittest.TestCase):
    def test_counter_deltas_exclude_preexisting_traffic(self):
        result = perf.bandwidth(
            ",bytes_in,bytes_out,\nserver.12,0,1000,\n"
            ",bytes_in,bytes_out,\nserver.12,0,1200,\nserver.12,0,1800,\n", 2)
        self.assertEqual(result["average_bytes_per_second"], 200)
        self.assertEqual(result["observed_seconds"], 4)
        self.assertNotIn("fps", result)

    def test_restart_or_multiple_processes_cannot_form_a_rate(self):
        for value in ["s.1,0,100\ns.1,0,99", "s.1,0,100\ns.2,0,200"]:
            with self.assertRaises(ValueError):
                perf.bandwidth(value, 1)

    def test_invalid_intervals_or_incomplete_samples_are_rejected(self):
        for interval in [0, -1, float("nan"), float("inf")]:
            with self.assertRaises(ValueError):
                perf.bandwidth("s.1,0,100\ns.1,0,200", interval)
        with self.assertRaises(ValueError):
            perf.bandwidth("s.1,0,100", 1)
