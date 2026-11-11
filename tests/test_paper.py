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

    def test_on_setup_prevents_duplicate_active_positions(self):
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
        pos1 = self.trader.on_setup(sig, "EUR_USD", "M15")
        self.assertIsNotNone(pos1)
        # Attempt to create duplicate setup on same pair & timeframe while first is PENDING
        pos2 = self.trader.on_setup(sig, "EUR_USD", "M15")
        self.assertIsNone(pos2)
        self.assertEqual(len(self.trader.positions), 1)

    def test_broker_interface_methods(self):
        resp = self.trader.create_limit_order("EUR_USD", "buy", 1.0850, 1.0830, 1.0890, 0.1)
        self.assertIn("orderCreateTransaction", resp)

        sig = Signal(
            kind="setup", time=datetime.now(timezone.utc), setup_time=datetime.now(timezone.utc),
            side="buy", entry=1.0850, stop=1.0830, target=1.0890, gap_low=1.0845, gap_high=1.0855,
            expires=datetime.now(timezone.utc), reason="Setup",
        )
        pos = self.trader.on_setup(sig, "EUR_USD", "M15")
        pending = self.trader.get_pending_orders()
        self.assertEqual(len(pending), 1)

        self.trader.cancel_order(pos.id)
        self.assertEqual(self.trader.positions[0].status, "CANCELLED")

    def test_break_even_at_plus_one_r_and_closed_be(self):
        now = datetime.now(timezone.utc)
        sig = Signal(
            kind="setup",
            time=now,
            setup_time=now,
            side="buy",
            entry=1.08500,
            stop=1.08300,  # 1.0R distance = 0.00200
            target=1.08900,  # 2.0R target
            gap_low=1.08450,
            gap_high=1.08550,
            expires=now,
            reason="Confirmed setup",
        )
        pos = self.trader.on_setup(sig, "EUR_USD", "M15")
        self.assertEqual(pos.stop, 1.08300)
        self.assertFalse(pos.is_break_even)

        # 1. Fill order
        self.trader.on_candle("EUR_USD", candle_high=1.08600, candle_low=1.08490, candle_time=now)
        self.assertEqual(pos.status, "OPEN")

        # 2. Price hits +1.0R (1.08500 + 0.00200 = 1.08700)
        events = self.trader.on_candle("EUR_USD", candle_high=1.08720, candle_low=1.08550, candle_time=now)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "BREAK_EVEN")
        self.assertTrue(pos.is_break_even)
        self.assertEqual(pos.stop, 1.08500)  # Stop moved to entry!

        # 3. Price retraces back down to entry (1.08500) -> hit break-even stop
        events_be = self.trader.on_candle("EUR_USD", candle_high=1.08600, candle_low=1.08480, candle_time=now)
        self.assertEqual(len(events_be), 1)
        self.assertEqual(events_be[0]["result"], "BREAK_EVEN")
        self.assertEqual(pos.status, "BREAK_EVEN")
        self.assertEqual(pos.realized_r, 0.0)
        self.assertEqual(pos.realized_pnl, 0.0)
        self.assertEqual(self.trader.balance, 10000.0)  # Capital fully protected!

        m = self.trader.get_metrics()
        self.assertEqual(m["break_evens"], 1)
        self.assertEqual(m["losses"], 0)
        self.assertEqual(m["total_r"], 0.0)


if __name__ == "__main__":
    unittest.main()

