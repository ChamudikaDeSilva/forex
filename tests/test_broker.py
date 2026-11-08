import json
import unittest
from unittest.mock import MagicMock, patch
import urllib.error

from smc_bot.broker import BrokerError, OandaBroker, format_price


class TestFormatPrice(unittest.TestCase):
    def test_fx_precision(self):
        self.assertEqual(format_price("EUR_USD", 1.085123), "1.08512")
        self.assertEqual(format_price("GBP_USD", 1.300000), "1.30000")

    def test_gold_precision(self):
        self.assertEqual(format_price("XAU_USD", 2715.456), "2715.46")


class TestOandaBroker(unittest.TestCase):
    def setUp(self):
        self.token = "valid-token-123"

    def test_init_validation(self):
        with self.assertRaises(ValueError):
            OandaBroker(token="")
        with self.assertRaises(ValueError):
            OandaBroker(token="token with spaces")

    def test_base_url_selection(self):
        practice_broker = OandaBroker(token=self.token, live=False)
        self.assertEqual(practice_broker.base_url, OandaBroker.PRACTICE_URL)

        live_broker = OandaBroker(token=self.token, live=True)
        self.assertEqual(live_broker.base_url, OandaBroker.LIVE_URL)

    @patch("smc_bot.broker.urllib.request.urlopen")
    def test_account_auto_discovery(self, mock_urlopen):
        resp_mock = MagicMock()
        resp_mock.read.return_value = json.dumps({
            "accounts": [{"id": "101-001-1234567-001"}]
        }).encode("utf-8")
        resp_mock.__enter__.return_value = resp_mock
        mock_urlopen.return_value = resp_mock

        broker = OandaBroker(token=self.token)
        acc_id = broker.account_id
        self.assertEqual(acc_id, "101-001-1234567-001")

    @patch("smc_bot.broker.urllib.request.urlopen")
    def test_calculate_units_eur_usd(self, mock_urlopen):
        resp_mock = MagicMock()
        resp_mock.read.return_value = json.dumps({
            "account": {"NAV": "10000.00", "currency": "USD"}
        }).encode("utf-8")
        resp_mock.__enter__.return_value = resp_mock
        mock_urlopen.return_value = resp_mock

        broker = OandaBroker(token=self.token, account_id="101-001-1234567-001")
        # NAV $10,000, 1% risk = $100. Stop distance = 0.00200 -> 50,000 units
        buy_units = broker.calculate_units("EUR_USD", "buy", entry=1.08500, stop=1.08300, risk_pct=1.0)
        self.assertEqual(buy_units, 50000)

        # For sell, units must be negative
        sell_units = broker.calculate_units("EUR_USD", "sell", entry=1.08500, stop=1.08700, risk_pct=1.0)
        self.assertEqual(sell_units, -50000)

    @patch("smc_bot.broker.urllib.request.urlopen")
    def test_calculate_units_gold(self, mock_urlopen):
        resp_mock = MagicMock()
        resp_mock.read.return_value = json.dumps({
            "account": {"NAV": "10000.00", "currency": "USD"}
        }).encode("utf-8")
        resp_mock.__enter__.return_value = resp_mock
        mock_urlopen.return_value = resp_mock

        broker = OandaBroker(token=self.token, account_id="101-001-1234567-001")
        # NAV $10,000, 1% risk = $100. Stop distance = $10.00 -> 10 units
        units = broker.calculate_units("XAU_USD", "buy", entry=2700.00, stop=2690.00, risk_pct=1.0)
        self.assertEqual(units, 10)

    def test_calculate_units_validation(self):
        broker = OandaBroker(token=self.token, account_id="101-001-1234567-001")
        with self.assertRaises(ValueError):
            broker.calculate_units("INVALID_PAIR", "buy", entry=1.0, stop=0.9)
        with self.assertRaises(ValueError):
            broker.calculate_units("EUR_USD", "buy", entry=1.0, stop=1.0)  # zero distance
        with self.assertRaises(ValueError):
            broker.calculate_units("EUR_USD", "buy", entry=1.0, stop=0.9, risk_pct=50)  # excessive risk

    @patch("smc_bot.broker.urllib.request.urlopen")
    def test_create_limit_order(self, mock_urlopen):
        resp_mock = MagicMock()
        resp_mock.read.return_value = json.dumps({
            "orderCreateTransaction": {"id": "999", "type": "LIMIT_ORDER"}
        }).encode("utf-8")
        resp_mock.__enter__.return_value = resp_mock
        mock_urlopen.return_value = resp_mock

        broker = OandaBroker(token=self.token, account_id="101-001-1234567-001")
        res = broker.create_limit_order(
            pair="EUR_USD",
            side="buy",
            entry=1.08500,
            stop=1.08300,
            target=1.08900,
            units=10000,
            client_tag="test_order",
        )
        self.assertEqual(res["orderCreateTransaction"]["id"], "999")

        # Verify posted payload
        call_req = mock_urlopen.call_args[0][0]
        data = json.loads(call_req.data.decode("utf-8"))
        self.assertEqual(data["order"]["instrument"], "EUR_USD")
        self.assertEqual(data["order"]["units"], "10000")
        self.assertEqual(data["order"]["price"], "1.08500")
        self.assertEqual(data["order"]["stopLossOnFill"]["price"], "1.08300")
        self.assertEqual(data["order"]["takeProfitOnFill"]["price"], "1.08900")

    @patch("smc_bot.broker.urllib.request.urlopen")
    def test_cancel_order(self, mock_urlopen):
        resp_mock = MagicMock()
        resp_mock.read.return_value = json.dumps({
            "orderCancelTransaction": {"id": "1000", "orderID": "999"}
        }).encode("utf-8")
        resp_mock.__enter__.return_value = resp_mock
        mock_urlopen.return_value = resp_mock

        broker = OandaBroker(token=self.token, account_id="101-001-1234567-001")
        res = broker.cancel_order("999")
        self.assertEqual(res["orderCancelTransaction"]["orderID"], "999")

    @patch("smc_bot.broker.urllib.request.urlopen")
    def test_close_all_trades(self, mock_urlopen):
        broker = OandaBroker(token=self.token, account_id="101-001-1234567-001")
        with patch.object(broker, "get_pending_orders", return_value=[{"id": "101"}, {"id": "102"}]), \
             patch.object(broker, "get_open_trades", return_value=[{"id": "201"}]), \
             patch.object(broker, "cancel_order", return_value={"status": "cancelled"}), \
             patch.object(broker, "close_trade", return_value={"status": "closed"}):
            results = broker.close_all_trades()
            self.assertEqual(len(results), 3)


if __name__ == "__main__":
    unittest.main()

