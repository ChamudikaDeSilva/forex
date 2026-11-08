import json
import unittest
from unittest.mock import MagicMock, patch

from smc_bot.telegram import TelegramClient, TelegramController, TelegramError


class TestTelegramClient(unittest.TestCase):
    def setUp(self):
        self.token = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"

    def test_init_validation(self):
        with self.assertRaises(ValueError):
            TelegramClient(token="")

    @patch("smc_bot.telegram.urllib.request.urlopen")
    def test_send_message(self, mock_urlopen):
        resp_mock = MagicMock()
        resp_mock.read.return_value = json.dumps({
            "ok": True,
            "result": {"message_id": 42}
        }).encode("utf-8")
        resp_mock.__enter__.return_value = resp_mock
        mock_urlopen.return_value = resp_mock

        client = TelegramClient(token=self.token)
        res = client.send_message(chat_id="9999", text="Hello Forex")
        self.assertEqual(res["message_id"], 42)

        # Verify POST payload
        req = mock_urlopen.call_args[0][0]
        data = json.loads(req.data.decode("utf-8"))
        self.assertEqual(data["chat_id"], "9999")
        self.assertEqual(data["text"], "Hello Forex")
        self.assertEqual(data["parse_mode"], "HTML")

    @patch("smc_bot.telegram.urllib.request.urlopen")
    def test_get_updates(self, mock_urlopen):
        resp_mock = MagicMock()
        resp_mock.read.return_value = json.dumps({
            "ok": True,
            "result": [{"update_id": 1, "message": {"text": "/status"}}]
        }).encode("utf-8")
        resp_mock.__enter__.return_value = resp_mock
        mock_urlopen.return_value = resp_mock

        client = TelegramClient(token=self.token)
        updates = client.get_updates(offset=0)
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]["update_id"], 1)


class TestTelegramController(unittest.TestCase):
    def setUp(self):
        self.client = MagicMock()
        self.autotrader = MagicMock()
        self.autotrader.paused = False
        self.autotrader.risk_pct = 1.0
        self.autotrader.pairs = ["EUR_USD"]
        self.autotrader.timeframes = ["M15"]
        self.autotrader.live = False

        self.controller = TelegramController(
            client=self.client,
            allowed_chat_ids={"12345"},
            autotrader=self.autotrader,
        )

    def test_unauthorized_user_blocked(self):
        msg = {
            "chat": {"id": 99999},
            "text": "/status"
        }
        self.controller._handle_message(msg)
        self.client.send_message.assert_called_once()
        args = self.client.send_message.call_args[0]
        self.assertEqual(args[0], 99999)
        self.assertIn("Access Denied", args[1])

    def test_status_command(self):
        msg = {
            "chat": {"id": "12345"},
            "text": "/status"
        }
        self.controller._handle_message(msg)
        self.client.send_message.assert_called_once()
        args = self.client.send_message.call_args[0]
        self.assertEqual(args[0], "12345")
        self.assertIn("Bot Status", args[1])

    def test_pause_and_resume_commands(self):
        self.controller._handle_message({"chat": {"id": "12345"}, "text": "/pause"})
        self.assertTrue(self.autotrader.paused)

        self.controller._handle_message({"chat": {"id": "12345"}, "text": "/resume"})
        self.assertFalse(self.autotrader.paused)

    def test_risk_update_command(self):
        self.controller._handle_message({"chat": {"id": "12345"}, "text": "/risk 2.5"})
        self.assertEqual(self.autotrader.risk_pct, 2.5)

    def test_closeall_command(self):
        self.autotrader.broker.close_all_trades.return_value = [{"type": "order_cancel"}]
        self.controller._handle_message({"chat": {"id": "12345"}, "text": "/closeall"})
        self.autotrader.broker.close_all_trades.assert_called_once()


if __name__ == "__main__":
    unittest.main()

