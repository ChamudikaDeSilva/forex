import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from smc_bot.state import State


class StateTests(unittest.TestCase):
    def test_round_trip_and_watermark_never_moves_backwards(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            time = datetime(2026, 1, 12, tzinfo=timezone.utc)
            state = State(path)
            self.assertIsNone(state.last("EUR_USD:M5"))
            state.advance("EUR_USD:M5", time)
            state.advance("EUR_USD:M5", time - timedelta(minutes=5))
            self.assertEqual(State(path).last("EUR_USD:M5"), time)
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_corrupt_or_naive_state_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            for content in ("{", "[]", json.dumps({"version": 1, "streams": {"EUR_USD:M5": "2026-01-12"}})):
                path.write_text(content)
                with self.assertRaisesRegex(ValueError, "Invalid state file"):
                    State(path)

    def test_failed_save_does_not_advance_the_in_memory_watermark(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            time = datetime(2026, 1, 12, tzinfo=timezone.utc)
            state = State(path)
            state.advance("EUR_USD:M5", time)
            with patch("smc_bot.state.os.replace", side_effect=OSError("disk unavailable")):
                with self.assertRaises(OSError):
                    state.advance("EUR_USD:M5", time + timedelta(minutes=5))
            self.assertEqual(state.last("EUR_USD:M5"), time)
            self.assertEqual(State(path).last("EUR_USD:M5"), time)


if __name__ == "__main__":
    unittest.main()
