import json
import time
import unittest
import urllib.request

from smc_bot.models import Candle
from smc_bot.mt5_bridge import (
    MT5BridgeServer,
    MT5Broker,
    MT5Feed,
    normalize_symbol,
    to_mt5_symbol,
)


class TestSymbolHelpers(unittest.TestCase):
    def test_normalize_symbol(self):
        self.assertEqual(normalize_symbol("EURUSD"), "EUR_USD")
        self.assertEqual(normalize_symbol("GBPUSD"), "GBP_USD")
        self.assertEqual(normalize_symbol("XAUUSD"), "XAU_USD")
        self.assertEqual(normalize_symbol("EUR_USD"), "EUR_USD")

    def test_to_mt5_symbol(self):
        self.assertEqual(to_mt5_symbol("EUR_USD"), "EURUSD")
        self.assertEqual(to_mt5_symbol("GBP_USD"), "GBPUSD")
        self.assertEqual(to_mt5_symbol("XAU_USD"), "XAUUSD")


class TestMT5BridgeAndBroker(unittest.TestCase):
    def setUp(self):
        self.bridge = MT5BridgeServer(host="127.0.0.1", port=5099)
        self.bridge.start()
        self.broker = MT5Broker(self.bridge)

    def tearDown(self):
        self.bridge.stop()

    def test_health_endpoint(self):
        url = "http://127.0.0.1:5099/health"
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data["status"], "ok")

    def test_sync_updates_account_and_dispatches_commands(self):
        # Queue an order command first
        self.broker.create_limit_order(
            pair="EUR_USD",
            side="buy",
            entry=1.08500,
            stop=1.08300,
            target=1.08900,
            units=0.5,
        )

        # Simulate MT5 sending sync request
        payload = {
            "account": {
                "login": "1715568067",
                "balance": 10000.0,
                "equity": 10050.0,
                "currency": "USD",
                "margin": 100.0,
                "margin_free": 9950.0,
            },
            "trades": [
                {"id": "1001", "instrument": "EURUSD", "currentUnits": 0.5, "price": 1.0850, "unrealizedPL": 50.0}
            ],
            "orders": [],
        }

        req = urllib.request.Request(
            "http://127.0.0.1:5099/api/sync",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data["status"], "ok")
            commands = data.get("commands", [])
            self.assertEqual(len(commands), 1)
            self.assertEqual(commands[0]["action"], "ORDER_LIMIT")
            self.assertEqual(commands[0]["symbol"], "EURUSD")
            self.assertEqual(commands[0]["lots"], 0.5)

        # Check that server state is updated
        summary = self.broker.get_account_summary()
        self.assertEqual(summary["account_id"], "1715568067")
        self.assertEqual(summary["NAV"], 10050.0)
        self.assertEqual(summary["unrealizedPL"], 50.0)

    def test_calculate_lots_eurusd(self):
        self.bridge.account_info = {
            "balance": 10000.0,
            "equity": 10000.0,
            "currency": "USD",
        }
        # $100 risk / (100,000 * 0.00200) = 100 / 200 = 0.50 lots
        lots = self.broker.calculate_units("EUR_USD", "buy", entry=1.08500, stop=1.08300, risk_pct=1.0)
        self.assertEqual(lots, 0.5)

    def test_calculate_lots_gold(self):
        self.bridge.account_info = {
            "balance": 10000.0,
            "equity": 10000.0,
            "currency": "USD",
        }
        # $100 risk / (100 * $10.00) = 100 / 1000 = 0.10 lots
        lots = self.broker.calculate_units("XAU_USD", "buy", entry=2700.00, stop=2690.00, risk_pct=1.0)
        self.assertEqual(lots, 0.1)

    def test_close_all_queues_command(self):
        self.broker.close_all_trades()
        self.assertEqual(len(self.bridge.queued_commands), 1)
        self.assertEqual(self.bridge.queued_commands[0]["action"], "CLOSE_ALL")


if __name__ == "__main__":
    unittest.main()

