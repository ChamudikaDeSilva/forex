import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse

from smc_bot.feeds import FeedError, OandaFeed, load_bundle, load_csv


class CsvFeedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.path = self.directory / "candles.csv"

    def write(self, rows, header="time,open,high,low,close"):
        self.path.write_text(header + "\n" + "\n".join(rows) + "\n", encoding="utf-8")

    def test_reads_candle_close_times_and_normalizes_timezone(self):
        self.write([
            "2026-01-01T05:35:00+05:30,1.1,1.3,1.0,1.2",
            "2026-01-01T00:10:00Z,1.2,1.4,1.1,1.3",
        ])
        candles = load_csv(self.path)
        self.assertEqual(len(candles), 2)
        self.assertEqual(candles[0].time, datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc))
        self.assertEqual(candles[1].close, 1.3)

    def test_rejects_timezone_free_timestamp_with_filename_and_row(self):
        self.write(["2026-01-01T00:05:00,1.1,1.3,1.0,1.2"])
        with self.assertRaisesRegex(ValueError, "candles.csv: row 2: .*timezone"):
            load_csv(self.path)

    def test_rejects_invalid_prices(self):
        for prices in (
            "1.1,1.0,0.9,1.2",  # Close is above the high.
            "1.1,1.3,1.2,1.2",  # Open is below the low.
            "nan,1.3,1.0,1.2",
            "1.1,inf,1.0,1.2",
            "0,1.3,0,1.2",
            "bad,1.3,1.0,1.2",
        ):
            with self.subTest(prices=prices):
                self.write([f"2026-01-01T00:05:00Z,{prices}"])
                with self.assertRaisesRegex(ValueError, "candles.csv: row 2:"):
                    load_csv(self.path)

    def test_rejects_duplicate_or_descending_timestamps(self):
        for timestamp in ("2026-01-01T00:05:00Z", "2026-01-01T00:00:00Z"):
            with self.subTest(timestamp=timestamp):
                self.write([
                    "2026-01-01T00:05:00Z,1.1,1.3,1.0,1.2",
                    f"{timestamp},1.2,1.4,1.1,1.3",
                ])
                with self.assertRaisesRegex(ValueError, "candles.csv: row 3: .*increasing"):
                    load_csv(self.path)

    def test_rejects_duplicate_instants_with_different_offsets(self):
        self.write([
            "2026-01-01T00:05:00Z,1.1,1.3,1.0,1.2",
            "2026-01-01T05:35:00+05:30,1.2,1.4,1.1,1.3",
        ])
        with self.assertRaisesRegex(ValueError, "strictly increasing"):
            load_csv(self.path)

    def test_rejects_bad_header_or_empty_data(self):
        self.write([], header="date,open,high,low,close")
        with self.assertRaisesRegex(ValueError, "row 1: expected CSV header"):
            load_csv(self.path)
        self.write([])
        with self.assertRaisesRegex(ValueError, "CSV contains no candles"):
            load_csv(self.path)

    def test_rejects_missing_or_extra_columns(self):
        for row in (
            "2026-01-01T00:05:00Z,1.1,1.3,1.0",
            "2026-01-01T00:05:00Z,1.1,1.3,1.0,1.2,extra",
        ):
            with self.subTest(row=row):
                self.write([row])
                with self.assertRaisesRegex(ValueError, "row 2: expected exactly five"):
                    load_csv(self.path)

    def test_load_bundle_requires_all_three_trend_timeframes(self):
        pair_directory = self.directory / "EUR_USD"
        pair_directory.mkdir()
        data = "time,open,high,low,close\n2026-01-01T00:05:00Z,1.1,1.3,1.0,1.2\n"
        for frame in ("M5", "M15", "H1", "H4", "D"):
            (pair_directory / f"{frame}.csv").write_text(data, encoding="utf-8")
        bundle = load_bundle(self.directory, "EUR_USD", ("M5", "M15"))
        self.assertEqual(set(bundle), {"M5", "M15", "H1", "H4", "D"})
        (pair_directory / "H4.csv").unlink()
        with self.assertRaises(FileNotFoundError) as error:
            load_bundle(self.directory, "EUR_USD", ("M5",))
        self.assertIn("H4.csv", str(error.exception))

    def test_rejects_unsupported_bundle_arguments(self):
        for pair, frames in (("../../other", ("M5",)), ("EUR_USD", ()), ("EUR_USD", ("H1",))):
            with self.subTest(pair=pair, frames=frames):
                with self.assertRaises(ValueError):
                    load_bundle(self.directory, pair, frames)


class OandaFeedTests(unittest.TestCase):
    def setUp(self):
        self.feed = OandaFeed("practice-token", count=300)
        self.pace = patch.object(self.feed, "_pace")
        self.pace.start()
        self.addCleanup(self.pace.stop)

    @staticmethod
    def row(timestamp="2026-01-01T00:00:00.000000000Z", complete=True):
        return {
            "time": timestamp,
            "complete": complete,
            "mid": {"o": "1.1", "h": "1.3", "l": "1.0", "c": "1.2"},
        }

    def transport(self, payload):
        response = Mock()
        response.read.return_value = json.dumps(payload).encode("utf-8")
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        return patch("smc_bot.feeds.urllib.request.urlopen", return_value=response)

    def test_requests_only_practice_midpoint_candles_with_utc_alignment(self):
        with self.transport({"candles": [self.row()]}) as transport:
            self.feed.candles("EUR_USD", "M5")
        request = transport.call_args.args[0]
        url = urlparse(request.full_url)
        self.assertEqual(url.scheme, "https")
        self.assertEqual(url.netloc, "api-fxpractice.oanda.com")
        self.assertEqual(url.path, "/v3/instruments/EUR_USD/candles")
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("Authorization"), "Bearer practice-token")
        self.assertEqual(parse_qs(url.query), {
            "granularity": ["M5"], "count": ["300"], "price": ["M"],
            "dailyAlignment": ["0"], "alignmentTimezone": ["UTC"],
        })
        self.assertEqual(transport.call_args.kwargs["timeout"], 20)

    def test_skips_incomplete_candles_and_converts_start_to_close(self):
        with self.transport({"candles": [
            self.row(),
            {"complete": False, "time": "unfinished", "mid": None},
        ]}):
            candles = self.feed.candles("EUR_USD", "M5")
        self.assertEqual(len(candles), 1)
        self.assertEqual(candles[0].time, datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc))

    def test_converts_h4_and_daily_closes_at_utc_boundaries(self):
        for frame, expected in (
            ("H4", datetime(2026, 1, 1, 4, tzinfo=timezone.utc)),
            ("D", datetime(2026, 1, 2, tzinfo=timezone.utc)),
        ):
            with self.subTest(frame=frame), self.transport({"candles": [self.row()]}):
                candle = self.feed.candles("XAU_USD", frame)[0]
            self.assertEqual(candle.time, expected)

    def test_preserves_microsecond_precision_from_nanosecond_timestamp(self):
        with self.transport({"candles": [self.row("2026-01-01T00:00:00.123456789Z")]}):
            candle = self.feed.candles("GBP_USD", "M15")[0]
        self.assertEqual(candle.time, datetime(2026, 1, 1, 0, 15, 0, 123456, tzinfo=timezone.utc))

    def test_bundle_includes_each_entry_frame_and_all_trend_filters(self):
        with patch.object(self.feed, "candles", return_value=[self.row()]) as candles:
            bundle = self.feed.bundle("GBP_USD", ("M5", "M15"))
        self.assertEqual(list(bundle), ["M5", "M15", "H1", "H4", "D"])
        self.assertEqual([call.args for call in candles.call_args_list], [
            ("GBP_USD", "M5"), ("GBP_USD", "M15"), ("GBP_USD", "H1"),
            ("GBP_USD", "H4"), ("GBP_USD", "D"),
        ])

    def test_authentication_failure_does_not_expose_token_or_error_body(self):
        failure = HTTPError("https://api-fxpractice.oanda.com", 401,
                            "private practice-token", {}, io.BytesIO(b"private practice-token"))
        with patch("smc_bot.feeds.urllib.request.urlopen", side_effect=failure):
            with self.assertRaises(FeedError) as error:
                self.feed.candles("EUR_USD", "M5")
        self.assertIn("HTTP 401", str(error.exception))
        self.assertIn("OANDA_API_TOKEN", str(error.exception))
        self.assertNotIn("practice-token", str(error.exception))
        self.assertNotIn("private", str(error.exception))
        self.assertTrue(error.exception.__suppress_context__)

    def test_network_failure_is_sanitized(self):
        with patch("smc_bot.feeds.urllib.request.urlopen", side_effect=URLError("private practice-token")):
            with self.assertRaisesRegex(FeedError, "connection failed or timed out") as error:
                self.feed.candles("EUR_USD", "M5")
        self.assertNotIn("practice-token", str(error.exception))

    def test_rejects_invalid_candles_and_nonincreasing_candle_times(self):
        invalid = self.row()
        invalid["mid"]["h"] = "1.0"
        for rows in ([invalid], [self.row(), self.row()], [self.row("2026-01-01T00:00:00")]):
            with self.subTest(rows=rows), self.transport({"candles": rows}):
                with self.assertRaisesRegex(FeedError, "invalid candle"):
                    self.feed.candles("EUR_USD", "M5")

    def test_rejects_empty_and_unexpected_responses(self):
        for payload, message in (({"candles": []}, "no completed candles"),
                                 ({"candles": [self.row(complete=False)]}, "no completed candles"),
                                 ({"message": "private"}, "no candle list")):
            with self.subTest(payload=payload), self.transport(payload):
                with self.assertRaisesRegex(FeedError, message):
                    self.feed.candles("EUR_USD", "M5")

    def test_validates_token_and_request_count_without_exposing_token(self):
        for token, count in (("", 300), ("secret\nother", 300), ("secret", 29),
                             ("secret", 5001), ("secret", 100.0), ("secret", True)):
            with self.subTest(count=count):
                with self.assertRaises(ValueError) as error:
                    OandaFeed(token, count)
                self.assertNotIn("secret", str(error.exception))

    def test_paces_requests_across_pairs_and_timeframes(self):
        feed = OandaFeed("practice-token")
        with patch("smc_bot.feeds.time.monotonic", side_effect=[10.0, 10.0, 10.1, 10.55]), \
                patch("smc_bot.feeds.time.sleep") as sleep:
            feed._pace()
            feed._pace()
        sleep.assert_called_once()
        self.assertAlmostEqual(sleep.call_args.args[0], 0.45)


if __name__ == "__main__":
    unittest.main()
