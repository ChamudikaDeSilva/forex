from datetime import datetime, timezone
import unittest
from unittest.mock import MagicMock

from smc_bot.autotrader import AutoTrader
from smc_bot.models import Signal


class TestAutoTrader(unittest.TestCase):
    def setUp(self):
        self.broker = MagicMock()
        self.telegram = MagicMock()
        self.chat_id = "123456"

        self.trader = AutoTrader(
            broker=self.broker,
            telegram_client=self.telegram,
            telegram_chat_id=self.chat_id,
            risk_pct=1.0,
            paused=False,
            dry_run=False,
            verbose=False,
        )

    def test_setup_signal_places_order_and_alerts(self):
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
            reason="Confirmed SMC setup",
        )

        self.broker.get_account_summary.return_value = {"NAV": "10000.00"}
        self.broker.calculate_units.return_value = 50000
        self.broker.create_limit_order.return_value = {
            "orderCreateTransaction": {"id": "123"}
        }

        self.trader.handle_signal(sig, "EUR_USD", "M15")

        self.broker.create_limit_order.assert_called_once()
        self.telegram.send_message.assert_called_once()
        msg_text = self.telegram.send_message.call_args[0][1]
        self.assertIn("SMC SETUP", msg_text)
        self.assertIn("Limit Order #123 Placed", msg_text)
        self.assertIn("50,000 units", msg_text)

    def test_cancelled_signal_cancels_pending_order(self):
        now = datetime.now(timezone.utc)
        sig_key = f"EUR_USD:M15:{now.isoformat()}:buy"
        self.trader.active_orders[sig_key] = "123"

        cancel_sig = Signal(
            kind="cancelled",
            time=now,
            setup_time=now,
            side="buy",
            entry=1.08500,
            stop=1.08300,
            target=1.08900,
            gap_low=1.08450,
            gap_high=1.08550,
            expires=now,
            reason="Stop touched",
        )

        self.trader.handle_signal(cancel_sig, "EUR_USD", "M15")

        self.broker.cancel_order.assert_called_once_with("123")
        self.assertNotIn(sig_key, self.trader.active_orders)
        self.telegram.send_message.assert_called_once()
        msg_text = self.telegram.send_message.call_args[0][1]
        self.assertIn("SETUP CANCELLED", msg_text)
        self.assertIn("Cancelled pending order #123", msg_text)

    def test_paused_mode_skips_broker_order(self):
        self.trader.paused = True
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
            reason="Confirmed SMC setup",
        )

        self.trader.handle_signal(sig, "EUR_USD", "M15")
        self.broker.create_limit_order.assert_not_called()
        self.telegram.send_message.assert_called_once()
        msg_text = self.telegram.send_message.call_args[0][1]
        self.assertIn("Auto-Trading Paused", msg_text)

    def test_dry_run_mode_calculates_without_broker_order(self):
        self.trader.dry_run = True
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
            reason="Confirmed SMC setup",
        )

        self.broker.get_account_summary.return_value = {"NAV": "10000.00"}
        self.broker.calculate_units.return_value = 50000

        self.trader.handle_signal(sig, "EUR_USD", "M15")
        self.broker.create_limit_order.assert_not_called()
        self.telegram.send_message.assert_called_once()
        msg_text = self.telegram.send_message.call_args[0][1]
        self.assertIn("Simulated (Dry Run", msg_text)


if __name__ == "__main__":
    unittest.main()
