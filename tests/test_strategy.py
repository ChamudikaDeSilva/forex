import unittest
from datetime import datetime, timedelta, timezone

from smc_bot.models import Candle, Config
from smc_bot.strategy import scan, structure_bias


START = datetime(2026, 1, 5, 0, 0, tzinfo=timezone.utc)


def candle(index, open_, high, low, close):
    return Candle(START + timedelta(minutes=5 * index), open_, high, low, close)


def long_pattern():
    return [
        candle(0, 100, 101, 99, 100),
        candle(1, 100, 102, 98, 100),
        candle(2, 100, 101, 99, 100),
        candle(3, 99, 101, 97, 99.5),  # sweep/reclaim a previously confirmed low
        candle(4, 100, 104, 99, 103),  # later directional close above frozen 102
        candle(5, 103, 104, 102, 103),  # FVG 101..102 now confirmed
    ]


def mirror(candles):
    return [Candle(c.time, 200 - c.open, 200 - c.low, 200 - c.high, 200 - c.close)
            for c in candles]


def trend_pattern():
    return [
        candle(0, 10, 11, 9, 10),
        candle(1, 11, 13, 10, 11),
        candle(2, 10, 11, 8, 10),
        candle(3, 12, 14, 10, 12),
        candle(4, 11, 12, 9, 11),
        candle(5, 13, 15, 11, 13),
    ]


class StructureBiasTests(unittest.TestCase):
    def test_two_confirmed_higher_highs_and_lows(self):
        self.assertEqual(structure_bias(trend_pattern(), window=1), "bullish")
        self.assertEqual(structure_bias(mirror(trend_pattern()), window=1), "bearish")

    def test_latest_unconfirmed_extreme_is_not_a_swing(self):
        candles = trend_pattern() + [
            candle(6, 14, 16, 12, 14),
            candle(7, 13, 17, 7, 13),  # potential lower low has no right-side confirmation
        ]
        self.assertEqual(structure_bias(candles, window=1), "bullish")

    def test_insufficient_or_mixed_structure_is_neutral(self):
        self.assertEqual(structure_bias([], window=1), "neutral")
        self.assertEqual(structure_bias(trend_pattern()[:5], window=1), "neutral")
        candles = trend_pattern()
        candles[3] = candle(3, 11, 12.5, 10, 11)
        self.assertEqual(structure_bias(candles, window=1), "neutral")

    def test_equal_highs_do_not_create_a_strict_pivot(self):
        candles = trend_pattern()
        candles[0] = candle(0, 10, 13, 9, 10)
        self.assertEqual(structure_bias(candles, window=1), "neutral")

    def test_invalid_window_is_rejected(self):
        for window in (0, 1.5, True):
            with self.assertRaises(ValueError):
                structure_bias([], window=window)


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.config = Config(swing_window=1, stop_buffer=0.1, min_gap=0.05,
                             sweep_timeout=12, entry_timeout=3)

    def run_scan(self, candles, bias="bullish", config=None):
        callback = bias if callable(bias) else lambda _: bias
        return scan(candles, config or self.config, "M5", callback)

    def test_long_setup_then_observed_retest(self):
        candles = long_pattern() + [candle(6, 103, 104, 101.4, 102)]
        events = self.run_scan(candles)
        self.assertEqual([event.kind for event in events], ["setup", "retest"])
        setup, retest = events
        self.assertEqual(setup.time, candles[5].time)
        self.assertEqual(setup.setup_time, candles[5].time)
        self.assertEqual(setup.side, "buy")
        self.assertEqual((setup.gap_low, setup.gap_high, setup.entry), (101, 102, 101.5))
        self.assertAlmostEqual(setup.stop, 96.9)
        self.assertAlmostEqual(setup.target, 110.7)
        self.assertEqual(setup.expires, setup.time + timedelta(minutes=15))
        self.assertEqual(retest.time, candles[6].time)
        self.assertIn("not an order or fill", retest.reason)

    def test_short_is_exact_mirror(self):
        candles = mirror(long_pattern() + [candle(6, 103, 104, 101.4, 102)])
        events = self.run_scan(candles, bias="bearish")
        self.assertEqual([event.kind for event in events], ["setup", "retest"])
        self.assertEqual(events[0].side, "sell")
        self.assertEqual(events[0].entry, 98.5)
        self.assertAlmostEqual(events[0].stop, 103.1)
        self.assertAlmostEqual(events[0].target, 89.3)

    def test_full_history_matches_every_prefix(self):
        candles = long_pattern() + [candle(6, 103, 104, 101.4, 102),
                                    candle(7, 102, 200, 50, 100)]
        full = self.run_scan(candles)
        for length in range(1, len(candles) + 1):
            expected = [event for event in full if event.time <= candles[length - 1].time]
            self.assertEqual(self.run_scan(candles[:length]), expected, f"prefix {length}")

    def test_fvg_confirmation_needs_third_candle(self):
        self.assertEqual(self.run_scan(long_pattern()[:5]), [])
        events = self.run_scan(long_pattern())
        self.assertEqual([event.kind for event in events], ["setup"])

    def test_neutral_and_opposite_trend_block_setups(self):
        for bias in ("neutral", "bearish"):
            self.assertEqual(self.run_scan(long_pattern(), bias=bias), [])

    def test_neutral_before_fvg_completion_cancels_candidate(self):
        candles = long_pattern()
        self.assertEqual(self.run_scan(candles, lambda time: "neutral" if time == candles[5].time
                                      else "bullish"), [])

    def test_alignment_is_queried_only_at_current_close(self):
        candles = long_pattern()
        queried = []
        def bias_at(time):
            queried.append(time)
            return "bullish"
        self.run_scan(candles, bias=bias_at)
        self.assertEqual(queried, [c.time for c in candles])

    def test_neutral_cancels_plan_before_retest(self):
        candles = long_pattern() + [candle(6, 103, 104, 101, 102)]
        events = self.run_scan(candles, lambda time: "neutral" if time == candles[6].time
                               else "bullish")
        self.assertEqual([event.kind for event in events], ["setup", "cancelled"])
        self.assertIn("trend alignment", events[-1].reason)

    def test_stop_and_entry_on_same_bar_cancel(self):
        candles = long_pattern() + [candle(6, 103, 104, 96.8, 102)]
        events = self.run_scan(candles)
        self.assertEqual([event.kind for event in events], ["setup", "cancelled"])
        self.assertIn("Stop touched", events[-1].reason)

    def test_opening_gap_beyond_entry_cancels(self):
        candles = long_pattern() + [candle(6, 101, 103, 100, 102)]
        events = self.run_scan(candles)
        self.assertEqual([event.kind for event in events], ["setup", "cancelled"])
        self.assertIn("opened beyond entry", events[-1].reason)

    def test_expiry_counts_bars_and_last_bar_can_retest(self):
        quiet = [candle(i, 103, 104, 102, 103) for i in range(6, 9)]
        events = self.run_scan(long_pattern() + quiet)
        self.assertEqual([event.kind for event in events], ["setup", "expired"])
        self.assertEqual(events[-1].time, quiet[-1].time)
        quiet[-1] = candle(8, 103, 104, 101.5, 102)
        self.assertEqual([event.kind for event in self.run_scan(long_pattern() + quiet)],
                         ["setup", "retest"])

    def test_weekend_extends_actual_expiry_but_not_nominal_estimate(self):
        candles = long_pattern()
        for index in range(6, 9):
            c = candle(index, 103, 104, 102, 103)
            candles.append(Candle(c.time + timedelta(days=2), c.open, c.high, c.low, c.close))
        events = self.run_scan(candles)
        self.assertEqual(events[-1].kind, "expired")
        self.assertEqual(events[-1].time, candles[-1].time)
        self.assertGreater(events[-1].time, events[-1].expires)

    def test_wick_crossing_and_opposite_body_are_not_structure_breaks(self):
        for replacement in (candle(4, 100, 104, 99, 101), candle(4, 104, 105, 99, 103)):
            candles = long_pattern()
            candles[4] = replacement
            self.assertEqual(self.run_scan(candles), [])

    def test_gap_too_small_is_rejected(self):
        candles = long_pattern()
        candles[5] = candle(5, 103, 104, 101.01, 103)
        self.assertEqual(self.run_scan(candles), [])

    def test_sweep_and_structure_break_cannot_share_a_candle(self):
        candles = long_pattern()
        candles[3] = candle(3, 99, 104, 97, 103)
        self.assertEqual(self.run_scan(candles), [])

    def test_pivot_confirmed_on_sweep_candle_is_not_yet_usable(self):
        candles = long_pattern()
        candles[2] = candle(2, 99, 101, 97, 99.5)
        self.assertEqual(self.run_scan(candles), [])

    def test_later_pivot_does_not_rewrite_frozen_structure_level(self):
        candles = long_pattern()[:4] + [
            candle(4, 99, 101.5, 98, 100),
            candle(5, 100, 100.5, 98.5, 100),
            candle(6, 100, 102, 99, 101.8),  # clears new 101.5 swing, not frozen 102
            candle(7, 101, 104, 100, 103),
            candle(8, 103, 104, 102.5, 103),
        ]
        events = self.run_scan(candles)
        self.assertEqual([event.kind for event in events], ["setup"])
        self.assertEqual(events[0].time, candles[8].time)

    def test_structure_deadline_is_inclusive(self):
        config = Config(swing_window=1, stop_buffer=0.1, min_gap=0.05,
                        sweep_timeout=1, entry_timeout=3)
        self.assertEqual([event.kind for event in self.run_scan(long_pattern(), config=config)],
                         ["setup"])
        candles = long_pattern()[:4] + [
            candle(4, 100, 101, 98, 100),
            candle(5, 100, 104, 99, 103),
            candle(6, 103, 104, 102, 103),
        ]
        self.assertEqual(self.run_scan(candles, config=config), [])

    def test_duplicate_or_decreasing_times_are_rejected(self):
        candles = long_pattern()
        for invalid in ([candles[0], candles[0]], [candles[1], candles[0]]):
            with self.assertRaises(ValueError):
                self.run_scan(invalid)
            with self.assertRaises(ValueError):
                structure_bias(invalid, window=1)

    def test_premium_discount_gate_blocks_buy_in_premium(self):
        from smc_bot.regime import MarketRegime
        candles = long_pattern()
        regime = MarketRegime(
            time=candles[-1].time,
            macro_bias="bullish",
            intermediate_bias="bullish",
            phase="EXPANSION",
            active_side="buy",
            classification="PRO_TREND",
            risk_factor=1.0,
            equilibrium=90.0,
        )
        high_cfg = Config(swing_window=1, stop_buffer=0.1, min_gap=0.05, sweep_timeout=12, entry_timeout=3, quality="high")
        events = scan(candles, high_cfg, "M5", lambda _: regime)
        self.assertEqual(events, [])

    def test_premium_discount_gate_allows_buy_in_discount(self):
        from smc_bot.regime import MarketRegime
        candles = long_pattern()
        regime = MarketRegime(
            time=candles[-1].time,
            macro_bias="bullish",
            intermediate_bias="bullish",
            phase="EXPANSION",
            active_side="buy",
            classification="PRO_TREND",
            risk_factor=1.0,
            equilibrium=110.0,
        )
        high_cfg = Config(swing_window=1, stop_buffer=0.1, min_gap=0.05, sweep_timeout=12, entry_timeout=3, quality="high")
        events = scan(candles, high_cfg, "M5", lambda _: regime)
        self.assertEqual([e.kind for e in events], ["setup"])
        self.assertEqual(events[0].side, "buy")

    def test_standard_quality_ignores_premium_discount_gate(self):
        from smc_bot.regime import MarketRegime
        candles = long_pattern()
        regime = MarketRegime(
            time=candles[-1].time,
            macro_bias="bullish",
            intermediate_bias="bullish",
            phase="EXPANSION",
            active_side="buy",
            classification="PRO_TREND",
            risk_factor=1.0,
            equilibrium=90.0,
        )
        std_cfg = Config(swing_window=1, stop_buffer=0.1, min_gap=0.05, sweep_timeout=12, entry_timeout=3, quality="standard")
        events = scan(candles, std_cfg, "M5", lambda _: regime)
        self.assertEqual([e.kind for e in events], ["setup"])


if __name__ == "__main__":
    unittest.main()
