from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from zoneinfo import ZoneInfo

from schedule import DailyState, load_config, window, project_lock

ROOT = Path(__file__).resolve().parents[1]


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config(ROOT / "config.example.json")

    def test_swiss_summer_and_winter(self):
        for day, hour in (("2026-10-05", 18), ("2026-12-05", 17)):
            current = datetime.fromisoformat(f"{day}T16:00:00+00:00")
            start, end, key = window(self.config, current)
            swiss = start.astimezone(ZoneInfo("Europe/Zurich"))
            self.assertEqual((swiss.hour, swiss.minute), (hour, 55))
            self.assertEqual((end.hour, end.minute), (20, 15))
            self.assertEqual(key, day)

    def test_delayed_opening_inside_window(self):
        current = datetime.fromisoformat("2026-10-05T20:05:00+03:00")
        start, end, day = window(self.config, current)
        self.assertTrue(start <= current < end)
        self.assertEqual(day, "2026-10-05")

    def test_after_window_goes_to_tomorrow(self):
        current = datetime.fromisoformat("2026-10-05T20:15:00+03:00")
        _, _, day = window(self.config, current)
        self.assertEqual(day, "2026-10-06")

    def test_window_crossing_midnight(self):
        self.config["opening_time"] = "00:02"
        current = datetime.fromisoformat("2026-10-05T23:59:00+03:00")
        start, end, day = window(self.config, current)
        self.assertTrue(start <= current < end)
        self.assertEqual(day, "2026-10-06")

    def test_reservation_survives_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            state = DailyState(path)
            url = self.config["channel_url"]
            state.record(url, "2026-10-05", "reserved")
            restored = DailyState(path)
            self.assertTrue(restored.blocked(url, "2026-10-05"))
            self.assertFalse(restored.blocked(url, "2026-10-06"))

    def test_invalid_config_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            for value in (0, True, float("nan"), 61):
                config = dict(self.config, check_interval_seconds=value)
                path.write_text(json.dumps(config), encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_config(path)

    def test_only_one_instance_then_lock_released(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "instance.lock"
            with project_lock(path):
                with self.assertRaises(ValueError):
                    with project_lock(path):
                        self.fail("Une deuxième instance ne doit pas démarrer.")
            with project_lock(path):
                pass


if __name__ == "__main__":
    unittest.main()
