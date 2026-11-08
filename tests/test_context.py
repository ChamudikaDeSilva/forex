import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from smc_bot.context import AlignedBias
from smc_bot.models import Candle, TREND_TIMEFRAMES


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.time = datetime(2026, 1, 12, tzinfo=timezone.utc)

    def candle(self, time):
        return Candle(time, 1.1, 1.2, 1.0, 1.15)

    def test_never_passes_future_candles_to_structure_detector(self):
        bundle = {tf: [self.candle(self.time), self.candle(self.time + timedelta(hours=1))]
                  for tf in TREND_TIMEFRAMES}
        with patch("smc_bot.context.structure_bias", return_value="bullish") as detector:
            bias = AlignedBias(bundle)
            self.assertEqual(bias(self.time), "bullish")
            self.assertTrue(all(len(call.args[0]) == 1 for call in detector.call_args_list))

    def test_disagreement_and_missing_history_block_signals(self):
        bundle = {tf: [self.candle(self.time)] for tf in TREND_TIMEFRAMES}
        with patch("smc_bot.context.structure_bias", side_effect=["bullish", "bullish", "bearish"]):
            self.assertEqual(AlignedBias(bundle)(self.time), "neutral")
        bundle["D"] = []
        with patch("smc_bot.context.structure_bias", return_value="bullish"):
            self.assertEqual(AlignedBias(bundle)(self.time), "neutral")

    def test_stale_hourly_history_blocks_signals(self):
        bundle = {tf: [self.candle(self.time)] for tf in TREND_TIMEFRAMES}
        with patch("smc_bot.context.structure_bias", return_value="bullish"):
            self.assertEqual(AlignedBias(bundle)(self.time + timedelta(hours=4)), "neutral")


if __name__ == "__main__":
    unittest.main()
