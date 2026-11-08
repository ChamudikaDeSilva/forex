import json
import unittest
from unittest.mock import MagicMock, patch

from smc_bot.feeds import YahooFeed
from smc_bot.models import Candle


class TestYahooFeed(unittest.TestCase):
    def setUp(self):
        self.feed = YahooFeed()

    @patch("smc_bot.feeds.urllib.request.urlopen")
    def test_candles_parsing(self, mock_urlopen):
        # 1700000000 = 2023-11-14 22:13:20 UTC
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "chart": {
                "result": [{
                    "timestamp": [1700000000, 1700000900],
                    "indicators": {
                        "quote": [{
                            "open": [1.0850, 1.0855],
                            "high": [1.0860, 1.0865],
                            "low": [1.0845, 1.0850],
                            "close": [1.0855, 1.0860],
                        }]
                    }
                }]
            }
        }).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        candles = self.feed.candles("EUR_USD", "M15")
        self.assertEqual(len(candles), 2)
        self.assertEqual(candles[0].open, 1.0850)
        self.assertEqual(candles[1].close, 1.0860)

    def test_aggregate_h4(self):
        from datetime import datetime, timezone, timedelta
        base = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
        h1_candles = [
            Candle(time=base + timedelta(hours=1), open=1.0, high=1.2, low=0.9, close=1.1),
            Candle(time=base + timedelta(hours=2), open=1.1, high=1.3, low=1.0, close=1.2),
            Candle(time=base + timedelta(hours=3), open=1.2, high=1.4, low=1.1, close=1.3),
            Candle(time=base + timedelta(hours=4), open=1.3, high=1.5, low=1.2, close=1.4),
        ]
        h4_candles = self.feed._aggregate_h4(h1_candles)
        self.assertEqual(len(h4_candles), 1)
        self.assertEqual(h4_candles[0].open, 1.0)
        self.assertEqual(h4_candles[0].high, 1.5)
        self.assertEqual(h4_candles[0].low, 0.9)
        self.assertEqual(h4_candles[0].close, 1.4)
        self.assertEqual(h4_candles[0].time, base + timedelta(hours=4))


if __name__ == "__main__":
    unittest.main()

