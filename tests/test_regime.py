import unittest
from datetime import datetime, timezone, timedelta

from smc_bot.models import Candle
from smc_bot.regime import HierarchicalRegimeDetector, MarketRegime


def make_candle(time: datetime, open_: float, high: float, low: float, close: float) -> Candle:
    return Candle(time=time, open=open_, high=high, low=low, close=close)


def make_bullish_candles(start_time: datetime, delta: timedelta) -> list[Candle]:
    # With window=1: swing highs at index 1 (13), index 3 (15). Swing lows at index 2 (8), index 4 (9).
    # Higher high (15 > 13) and higher low (9 > 8) -> bullish.
    specs = [
        (10, 11, 9, 10),
        (11, 13, 10, 11),
        (10, 11, 8, 10),
        (12, 15, 10, 12),
        (11, 12, 9, 11),
        (13, 16, 11, 14),
        (14, 15, 13, 14),
    ]
    return [make_candle(start_time + delta * (i + 1), o, h, l, c) for i, (o, h, l, c) in enumerate(specs)]


def make_bearish_candles(start_time: datetime, delta: timedelta) -> list[Candle]:
    bullish = make_bullish_candles(start_time, delta)
    return [make_candle(c.time, 100 - c.open, 100 - c.low, 100 - c.high, 100 - c.close) for c in bullish]


class TestHierarchicalRegimeDetector(unittest.TestCase):
    def setUp(self):
        base = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
        self.base = base

        # Macro: H4 bullish (7 candles of 4h -> up to 28h)
        self.h4_candles = make_bullish_candles(base, timedelta(hours=4))

        # Intermediate: H1 bearish (Retracement) extended to 28 hours so it is not stale
        self.h1_down = make_bearish_candles(base, timedelta(hours=1))
        for i in range(len(self.h1_down), 28):
            prev = self.h1_down[-1]
            self.h1_down.append(make_candle(base + timedelta(hours=i + 1), prev.open, prev.high, prev.low, prev.close))

        # Intermediate: H1 bullish (Expansion) extended to 28 hours
        self.h1_up = make_bullish_candles(base, timedelta(hours=1))
        for i in range(len(self.h1_up), 28):
            prev = self.h1_up[-1]
            self.h1_up.append(make_candle(base + timedelta(hours=i + 1), prev.open, prev.high, prev.low, prev.close))

    def test_retracement_phase_detection(self):
        bundle = {
            "H4": self.h4_candles,
            "H1": self.h1_down,
        }
        detector = HierarchicalRegimeDetector(
            bundle,
            macro_timeframes=("H4",),
            intermediate_timeframes=("H1",),
            swing_window=1,
        )
        eval_time = self.h4_candles[-1].time
        regime = detector.evaluate_at(eval_time)

        self.assertIsInstance(regime, MarketRegime)
        self.assertEqual(regime.macro_bias, "bullish")
        self.assertEqual(regime.intermediate_bias, "bearish")
        self.assertEqual(regime.phase, "RETRACEMENT")
        self.assertEqual(regime.active_side, "sell")
        self.assertEqual(regime.classification, "RETRACEMENT")
        self.assertEqual(regime.risk_factor, 0.5)

    def test_expansion_phase_detection(self):
        bundle = {
            "H4": self.h4_candles,
            "H1": self.h1_up,
        }
        detector = HierarchicalRegimeDetector(
            bundle,
            macro_timeframes=("H4",),
            intermediate_timeframes=("H1",),
            swing_window=1,
        )
        eval_time = self.h4_candles[-1].time
        regime = detector.evaluate_at(eval_time)

        self.assertEqual(regime.macro_bias, "bullish")
        self.assertEqual(regime.intermediate_bias, "bullish")
        self.assertEqual(regime.phase, "EXPANSION")
        self.assertEqual(regime.active_side, "buy")
        self.assertEqual(regime.classification, "PRO_TREND")
        self.assertEqual(regime.risk_factor, 1.0)


if __name__ == "__main__":
    unittest.main()
