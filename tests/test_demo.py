import tempfile
import unittest
from pathlib import Path

from smc_bot.context import AlignedBias
from smc_bot.demo import synthetic_bundle, write_samples
from smc_bot.feeds import load_bundle
from smc_bot.models import PAIRS, config_for
from smc_bot.strategy import scan


class DemoTests(unittest.TestCase):
    def test_csv_end_to_end_for_all_requested_pairs_and_entry_timeframes(self):
        with tempfile.TemporaryDirectory() as directory:
            write_samples(Path(directory))
            for pair in PAIRS:
                bundle = load_bundle(Path(directory), pair, ("M5", "M15"))
                bias = AlignedBias(bundle)
                for tf in ("M5", "M15"):
                    events = scan(bundle[tf], config_for(pair), tf, bias)
                    self.assertEqual([event.kind for event in events], ["setup", "retest"], (pair, tf))
                    setup = events[0]
                    self.assertLess(setup.stop, setup.entry)
                    self.assertAlmostEqual((setup.target - setup.entry) / (setup.entry - setup.stop), 2)

    def test_future_prices_do_not_change_earlier_demo_events(self):
        bundle = synthetic_bundle()
        bias = AlignedBias(bundle)
        for tf in ("M5", "M15"):
            all_events = scan(bundle[tf], config_for("EUR_USD"), tf, bias)
            for count in range(1, len(bundle[tf]) + 1):
                earlier = scan(bundle[tf][:count], config_for("EUR_USD"), tf, bias)
                self.assertEqual(earlier, [event for event in all_events
                                         if event.time <= bundle[tf][count - 1].time])


if __name__ == "__main__":
    unittest.main()
