from datetime import datetime, timezone
import json
import tempfile
import unittest
from pathlib import Path

from smc_bot.models import Signal
from smc_bot.paper import PaperTrader


class TestPaperTrader(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_file = Path(self.tmp.name) / "paper_trades.json"
        self.trader = PaperTrader(initial_balance=10000.0, risk_pct=1.0, state_file=self.state_file)

    def test_on_setup_creates_pending_order(self):
        now = datetime.now(timezone.utc)
        sig = Signal(
            kind="setup",
            time=now,
            setup_time=now,
            side="buy",
            entry=1.08500,
            stop=1.08300,
            target=1.08900,
            gap_low=1.08450,
            gap_high=1.08550,
            expires=now,
            reason="Confirmed setup",
        )
        pos = self.trader.on_setup(sig, "EUR_USD", "M15")
        self.assertEqual(pos.status, "PENDING")
        self.assertEqual(pos.reward_risk, 2.0)
        self.assertEqual(pos.risk_usd, 100.0)
        self.assertTrue(self.state_file.exists())

    def test_candle_fill_and_win(self):
        now = datetime.now(timezone.utc)
        sig = Signal(
            kind="setup",
            time=now,
            setup_time=now,
            side="buy",
            entry=1.08500,
            stop=1.08300,
            target=1.08900,
            gap_low=1.08450,
            gap_high=1.08550,
            expires=now,
            reason="Confirmed setup",
        )
        self.trader.on_setup(sig, "EUR_USD", "M15")

        # Candle 1 touches entry -> Filled
        events = self.trader.on_candle("EUR_USD", candle_high=1.08600, candle_low=1.08490, candle_time=now)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "FILLED")
        self.assertEqual(self.trader.positions[0].status, "OPEN")

        # Candle 2 hits target -> Win (+2.0R = +$200)
        events2 = self.trader.on_candle("EUR_USD", candle_high=1.08950, candle_low=1.08600, candle_time=now)
        self.assertEqual(len(events2), 1)
        self.assertEqual(events2[0]["result"], "WIN")
        self.assertEqual(self.trader.positions[0].status, "WIN")
        self.assertEqual(self.trader.balance, 10200.0)

        # Check performance metrics
        m = self.trader.get_metrics()
        self.assertEqual(m["total_trades"], 1)
        self.assertEqual(m["wins"], 1)
        self.assertEqual(m["win_rate"], 100.0)
        self.assertEqual(m["total_r"], 2.0)
        self.assertEqual(m["net_pnl"], 200.0)

    def test_candle_stop_loss(self):
        now = datetime.now(timezone.utc)
        sig = Signal(
            kind="setup",
            time=now,
            setup_time=now,
            side="buy",
            entry=1.08500,
            stop=1.08300,
            target=1.08900,
            gap_low=1.08450,
            gap_high=1.08550,
            expires=now,
            reason="Confirmed setup",
        )
        self.trader.on_setup(sig, "EUR_USD", "M15")

        # Fill order
        self.trader.on_candle("EUR_USD", candle_high=1.08600, candle_low=1.08490, candle_time=now)

        # Hit stop
        events = self.trader.on_candle("EUR_USD", candle_high=1.08450, candle_low=1.08250, candle_time=now)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["result"], "LOSS")
        self.assertEqual(self.trader.balance, 9900.0)

        m = self.trader.get_metrics()
        self.assertEqual(m["losses"], 1)
        self.assertEqual(m["win_rate"], 0.0)
        self.assertEqual(m["total_r"], -1.0)
        self.assertEqual(m["max_drawdown_pct"], 1.0)


if __name__ == "__main__":
    unittest.main()

