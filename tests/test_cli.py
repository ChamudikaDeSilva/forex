import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from smc_bot import cli
from smc_bot.feeds import FeedError
from smc_bot.models import Candle, Signal
from smc_bot.state import State


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state_path = Path(self.directory.name) / "state.json"
        self.now = datetime(2026, 1, 12, 12, tzinfo=timezone.utc)
        self.arguments = [
            "watch", "--once", "--json", "--pairs", "EUR_USD",
            "--timeframes", "M5", "--state", str(self.state_path),
        ]
        token = patch.dict(os.environ, {"OANDA_API_TOKEN": "mock-practice-token"})
        token.start()
        self.addCleanup(token.stop)
        clock = patch("smc_bot.cli.datetime")
        clock.start().now.return_value = self.now
        self.addCleanup(clock.stop)
        provider = patch("smc_bot.cli.OandaFeed")
        self.provider = provider.start()
        self.addCleanup(provider.stop)
        self.feed = self.provider.return_value

    def bundle(self, latest):
        candle = Candle(latest, 1.1, 1.2, 1.0, 1.15)
        return {frame: [candle] for frame in ("M5", "H1", "H4", "D")}

    def signal(self, time):
        return Signal(
            kind="setup", time=time, setup_time=time, side="buy",
            entry=1.15, stop=1.1, target=1.25, gap_low=1.14,
            gap_high=1.16, expires=time + timedelta(hours=1),
            reason="Synthetic alert for CLI verification",
        )

    def run_command(self, arguments=None):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            result = cli.main(self.arguments if arguments is None else arguments)
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        return result, records, errors.getvalue()

    def test_warmup_restart_and_new_event_emit_only_once(self):
        baseline = self.now - timedelta(minutes=5)
        historical = self.signal(self.now - timedelta(minutes=6))
        fresh = self.signal(self.now - timedelta(minutes=1))
        self.feed.bundle.return_value = self.bundle(baseline)
        with patch("smc_bot.cli.scan", return_value=[historical]) as scanner:
            result, records, messages = self.run_command()
            self.assertEqual(result, 0)
            self.assertEqual(records, [])
            self.assertIn("Warmed up EUR_USD M5", messages)
            self.assertEqual(State(self.state_path).last("EUR_USD:M5"), baseline)

            # A fresh process sees the persisted watermark and stays quiet.
            result, records, _ = self.run_command()
            self.assertEqual(result, 0)
            self.assertEqual(records, [])

            self.feed.bundle.return_value = self.bundle(fresh.time)
            scanner.return_value = [historical, fresh]
            result, records, messages = self.run_command()
            self.assertEqual(result, 0)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["time"], fresh.time.isoformat())
            self.assertEqual(records[0]["pair"], "EUR_USD")
            self.assertEqual(records[0]["timeframe"], "M5")
            self.assertNotIn("Warmed up", messages)
            self.assertEqual(State(self.state_path).last("EUR_USD:M5"), fresh.time)

            # Restarting again with the same last candle cannot replay it.
            result, records, _ = self.run_command()
            self.assertEqual(result, 0)
            self.assertEqual(records, [])
            self.assertEqual(scanner.call_count, 2)

    def test_catchup_skips_stale_alerts_but_emits_a_fresh_event(self):
        State(self.state_path).advance("EUR_USD:M5", self.now - timedelta(hours=1))
        stale = self.signal(self.now - timedelta(minutes=40))
        fresh = self.signal(self.now - timedelta(minutes=1))
        self.feed.bundle.return_value = self.bundle(fresh.time)
        with patch("smc_bot.cli.scan", return_value=[stale, fresh]):
            result, records, messages = self.run_command()
        self.assertEqual(result, 0)
        self.assertEqual([record["time"] for record in records], [fresh.time.isoformat()])
        self.assertIn("skipped 1 stale events", messages)
        self.assertEqual(State(self.state_path).last("EUR_USD:M5"), fresh.time)

    def test_once_provider_failure_returns_failure_without_checkpoint(self):
        self.feed.bundle.side_effect = FeedError("OANDA EUR_USD/M5: connection failed or timed out")
        with patch("smc_bot.cli.scan") as scanner:
            result, records, messages = self.run_command()
        self.assertEqual(result, 1)
        self.assertEqual(records, [])
        self.assertIn("connection failed or timed out", messages)
        self.assertFalse(self.state_path.exists())
        scanner.assert_not_called()

    def test_one_failed_pair_does_not_prevent_other_pairs_from_warming_up(self):
        good_bundle = self.bundle(self.now - timedelta(minutes=1))
        self.feed.bundle.side_effect = [FeedError("EUR_USD is temporarily unavailable"), good_bundle]
        arguments = [
            "watch", "--once", "--json", "--pairs", "EUR_USD", "GBP_USD",
            "--timeframes", "M5", "--state", str(self.state_path),
        ]
        with patch("smc_bot.cli.scan", return_value=[]):
            result, records, messages = self.run_command(arguments)
        self.assertEqual(result, 1)
        self.assertEqual(records, [])
        state = State(self.state_path)
        self.assertIsNone(state.last("EUR_USD:M5"))
        self.assertEqual(state.last("GBP_USD:M5"), good_bundle["M5"][-1].time)
        self.assertIn("Warmed up GBP_USD M5", messages)

    def test_missing_token_fails_before_creating_provider_or_prompting(self):
        with patch.dict(os.environ, {"OANDA_API_TOKEN": ""}), \
                patch("smc_bot.cli.sys.stdin.isatty", return_value=False), \
                patch("smc_bot.cli.getpass.getpass") as prompt:
            result, records, messages = self.run_command()
        self.assertEqual(result, 1)
        self.assertEqual(records, [])
        self.assertIn("Set OANDA_API_TOKEN", messages)
        self.provider.assert_not_called()
        prompt.assert_not_called()
        self.assertFalse(self.state_path.exists())

    def test_future_entry_candles_cannot_emit_alerts_or_advance_watermark(self):
        future = self.now + timedelta(minutes=5)
        self.feed.bundle.return_value = self.bundle(future)
        with patch("smc_bot.cli.scan", return_value=[self.signal(future)]):
            result, records, messages = self.run_command()
        self.assertEqual(result, 1)
        self.assertEqual(records, [])
        self.assertIn("candle close time is in the future", messages)
        self.assertFalse(self.state_path.exists())

    def test_invalid_runtime_options_fail_before_provider_creation(self):
        for option, value in (("--rr", "nan"), ("--interval", "5"), ("--count", "29")):
            with self.subTest(option=option):
                result, records, messages = self.run_command(self.arguments + [option, value])
                self.assertEqual(result, 1)
                self.assertEqual(records, [])
                self.assertIn("Error:", messages)
                self.provider.assert_not_called()


class AutotradeCliTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state_path = Path(self.directory.name) / "autotrade_state.json"

    @patch("smc_bot.cli.MT5BridgeServer")
    @patch("smc_bot.cli.run_autotrader")
    def test_autotrade_invokes_runner_with_arguments(self, mock_run, mock_bridge):
        mock_run.return_value = 0
        with patch.dict(os.environ, {"OANDA_API_TOKEN": "valid-token"}):
            cli.main([
                "autotrade",
                "--risk-pct", "1.5",
                "--pairs", "EUR_USD",
                "--timeframes", "M15",
                "--telegram-token", "bot123",
                "--telegram-chat-id", "chat456",
                "--state", str(self.state_path),
                "--dry-run",
            ])
            mock_run.assert_called_once()
            _, kwargs = mock_run.call_args
            self.assertEqual(kwargs["risk_pct"], 1.5)
            self.assertEqual(kwargs["pairs"], ["EUR_USD"])
            self.assertEqual(kwargs["timeframes"], ["M15"])
            self.assertTrue(kwargs["dry_run"])
            self.assertEqual(kwargs["telegram_chat_id"], "chat456")

    @patch("smc_bot.cli.run_autotrader")
    def test_autotrade_intraday_trend_and_m1_tuning(self, mock_run):
        mock_run.return_value = 0
        cli.main([
            "autotrade",
            "--broker", "paper",
            "--feed", "yahoo",
            "--timeframes", "M1", "M5",
            "--intraday-trend",
            "--state", str(self.state_path),
        ])
        mock_run.assert_called_once()
        _, kwargs = mock_run.call_args
        self.assertTrue(kwargs["intraday_trend"])
        self.assertEqual(kwargs["sweep_bars"], 24)
        self.assertEqual(kwargs["entry_bars"], 24)
        self.assertEqual(kwargs["fx_gap"], 0.00002)

    @patch("smc_bot.cli.run_autotrader")
    def test_autotrade_custom_trend_tf(self, mock_run):
        mock_run.return_value = 0
        cli.main([
            "autotrade",
            "--broker", "paper",
            "--feed", "yahoo",
            "--timeframes", "M1", "M5",
            "--trend-tf", "M15", "H1",
            "--state", str(self.state_path),
        ])
        mock_run.assert_called_once()
        _, kwargs = mock_run.call_args
        self.assertEqual(kwargs["trend_timeframes"], ["M15", "H1"])

    def test_load_dotenv(self):
        env_file = Path(self.directory.name) / ".env"
        env_file.write_text("TEST_CUSTOM_KEY='hello_test'\n# comment\nANOTHER_KEY=\"1234\"\n", encoding="utf-8")
        with patch.dict(os.environ, {}, clear=False):
            cli.load_dotenv(env_file)
            self.assertEqual(os.environ.get("TEST_CUSTOM_KEY"), "hello_test")
            self.assertEqual(os.environ.get("ANOTHER_KEY"), "1234")


if __name__ == "__main__":
    unittest.main()
