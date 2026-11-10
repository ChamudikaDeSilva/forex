import argparse
import getpass
import json
import os
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path

from .autotrader import run_autotrader
from .broker import OandaBroker
from .context import AlignedBias
from .feeds import FeedError, OandaFeed, YahooFeed, load_bundle
from .models import PAIRS, Config, Signal, config_for, duration
from .mt5_bridge import MT5BridgeServer, MT5Broker, MT5Feed
from .paper import PaperTrader
from .state import State
from .strategy import scan
from .telegram import TelegramClient


def load_dotenv(path: str | Path = ".env") -> None:
    """Load key-value environment variables from .env file if present."""
    env_file = Path(path)
    if not env_file.is_file():
        return
    with env_file.open("r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, val = stripped.split("=", 1)
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val


def emit(signal: Signal, pair: str, timeframe: str, json_output: bool = False) -> None:
    if json_output:
        record = asdict(signal)
        for key in ("time", "setup_time", "expires"):
            record[key] = record[key].isoformat()
        record.update(pair=pair, timeframe=timeframe, id=signal.key(pair, timeframe))
        print(json.dumps(record), flush=True)
        return
    digits = 2 if pair == "XAU_USD" else 5
    def price(value: float) -> str:
        return f"{value:.{digits}f}"
    print(f"[{signal.time.isoformat()}] {pair} {timeframe} {signal.kind.upper()} {signal.side.upper()}\n"
          f"  Planned entry {price(signal.entry)} | SL {price(signal.stop)} | TP {price(signal.target)}\n"
          f"  FVG {price(signal.gap_low)}–{price(signal.gap_high)} | "
          f"estimated expiry {signal.expires.isoformat()}\n"
          f"  {signal.reason}", flush=True)


def status(message: str, json_output: bool = False) -> None:
    print(message, file=sys.stderr if json_output else sys.stdout, flush=True)


def configuration(args: argparse.Namespace, pair: str) -> Config:
    gold = pair == "XAU_USD"
    return config_for(pair, swing_window=args.swing_window, reward_risk=args.rr,
                      sweep_timeout=args.sweep_bars, entry_timeout=args.entry_bars,
                      stop_buffer=args.gold_buffer if gold else args.fx_buffer,
                      min_gap=args.gold_gap if gold else args.fx_gap)


def replay(args: argparse.Namespace) -> int:
    total = 0
    for pair in args.pairs:
        bundle = load_bundle(args.data, pair, tuple(args.timeframes))
        bias = AlignedBias(bundle, args.swing_window)
        for tf in args.timeframes:
            events = scan(bundle[tf], configuration(args, pair), tf, bias)
            for event in events:
                emit(event, pair, tf, args.json)
            total += len(events)
    status(f"Replay complete: {total} events. This reports candle patterns; it does not calculate returns.", args.json)
    return 0


def watch(args: argparse.Namespace) -> int:
    state = State(args.state)
    if args.source == "oanda":
        token = os.environ.get("OANDA_API_TOKEN", "").strip()
        if not token and sys.stdin.isatty():
            token = getpass.getpass("OANDA practice API token: ").strip()
        if not token:
            raise ValueError("Set OANDA_API_TOKEN or run watch in a terminal to enter your practice token")
        feed = OandaFeed(token, count=args.count)
        get_bundle = feed.bundle
    else:
        get_bundle = lambda pair, timeframes: load_bundle(args.data, pair, timeframes)
    status("Watching completed candles. H1, H4 and D must agree. Ctrl+C stops the bot.", args.json)
    status("First poll warms up each new stream. SETUP is a planned entry; RETEST is a delayed candle observation.", args.json)
    while True:
        failures = 0
        started = time.monotonic()
        for pair in args.pairs:
            try:
                bundle = get_bundle(pair, tuple(args.timeframes))
                bias = AlignedBias(bundle, args.swing_window)
                for tf in args.timeframes:
                    candles = bundle[tf]
                    if not candles:
                        raise ValueError(f"{pair} {tf}: no completed candles")
                    latest = candles[-1].time
                    checkpoint = state.last(f"{pair}:{tf}")
                    if checkpoint is not None and latest <= checkpoint:
                        continue
                    events = scan(candles, configuration(args, pair), tf, bias)
                    now = datetime.now(timezone.utc)
                    if latest > now:
                        raise ValueError(f"{pair} {tf}: candle close time is in the future")
                    if checkpoint is None:
                        values = bias.values_at(latest)
                        status(f"Warmed up {pair} {tf} through {latest.isoformat()} | "
                               + " ".join(f"{key}={value}" for key, value in values.items()), args.json)
                    else:
                        fresh = now - 2 * duration(tf)
                        skipped = 0
                        for event in events:
                            if event.time > checkpoint:
                                if event.time >= fresh:
                                    emit(event, pair, tf, args.json)
                                else:
                                    skipped += 1
                        if skipped:
                            status(f"{pair} {tf}: skipped {skipped} stale events during catch-up", args.json)
                    state.advance(f"{pair}:{tf}", latest)
            except (ValueError, OSError, FeedError) as exc:
                failures += 1
                status(f"{pair}: {exc}", args.json)
        if args.once:
            return 1 if failures else 0
        elapsed = time.monotonic() - started
        time.sleep(max(1.0, args.interval - elapsed))


def add_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--pairs", nargs="+", choices=PAIRS, default=list(PAIRS))
    parser.add_argument("--timeframes", nargs="+", choices=("M1", "M5", "M15"), default=["M1", "M5"])
    parser.add_argument("--json", action="store_true", help="Print signal JSON lines; send status text to stderr")
    parser.add_argument("--rr", type=float, default=2.0, help="Planned reward/risk ratio (default 2)")
    parser.add_argument("--swing-window", type=int, default=2, help="Bars on each side confirming a swing")
    parser.add_argument("--sweep-bars", type=int, default=12, help="Bars allowed from sweep to structure break")
    parser.add_argument("--entry-bars", type=int, default=12, help="Completed bars allowed for an FVG retracement")
    parser.add_argument("--fx-buffer", type=float, default=0.0001, help="EUR/GBP stop buffer in price units")
    parser.add_argument("--gold-buffer", type=float, default=0.1, help="Gold stop buffer in dollars")
    parser.add_argument("--fx-gap", type=float, default=0.00005, help="Minimum EUR/GBP FVG width")
    parser.add_argument("--gold-gap", type=float, default=0.05, help="Minimum gold FVG width in dollars")


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="SMC entry plans with H1/H4/daily structure alignment")
    commands = parser.add_subparsers(dest="command", required=True)
    demo_parser = commands.add_parser("demo", help="Run a clearly labelled synthetic EUR/USD example")
    demo_parser.add_argument("--json", action="store_true")
    demo_parser.add_argument("--write-data", type=Path, help="Also write synthetic CSV files for all three instruments")
    replay_parser = commands.add_parser("replay", help="Replay completed-candle CSV history")
    replay_parser.add_argument("--data", type=Path, default=Path("data"))
    add_options(replay_parser)
    watch_parser = commands.add_parser("watch", help="Watch the OANDA practice API or updated CSV files")
    watch_parser.add_argument("--source", choices=("oanda", "csv"), default="oanda")
    watch_parser.add_argument("--data", type=Path, default=Path("data"))
    watch_parser.add_argument("--state", type=Path, default=Path("state.json"))
    watch_parser.add_argument("--count", type=int, default=300, help="OANDA candles per timeframe (30–5000)")
    watch_parser.add_argument("--interval", type=float, default=30, help="Poll interval in seconds (at least 10)")
    watch_parser.add_argument("--once", action="store_true", help="Run one poll and exit")
    add_options(watch_parser)

    autotrade_parser = commands.add_parser(
        "autotrade",
        help="Automated SMC execution on OANDA with real-time Telegram bot controls",
    )
    autotrade_parser.add_argument("--broker", choices=("paper", "mt5", "oanda"), default="paper", help="Execution mode: paper (default), mt5, or oanda")
    autotrade_parser.add_argument("--feed", choices=("yahoo", "oanda", "mt5"), default="yahoo", help="Market data source: yahoo (default), oanda, or mt5")
    autotrade_parser.add_argument("--initial-balance", type=float, default=10000.0, help="Paper trading initial balance (default $10,000)")
    autotrade_parser.add_argument("--bridge-port", type=int, default=5001, help="MT5 bridge server port (default 5001)")
    autotrade_parser.add_argument("--risk-pct", type=float, default=1.0, help="Risk percentage per trade (default 1.0%%)")
    autotrade_parser.add_argument("--account-id", type=str, default="", help="OANDA account ID (auto-detected if omitted)")
    autotrade_parser.add_argument("--telegram-token", type=str, default="", help="Telegram bot API token")
    autotrade_parser.add_argument("--telegram-chat-id", type=str, default="", help="Authorized Telegram chat ID")
    autotrade_parser.add_argument("--live", action="store_true", help="Trade on live broker account instead of practice")
    autotrade_parser.add_argument("--dry-run", action="store_true", help="Simulate orders and calculations without sending to broker")
    autotrade_parser.add_argument("--intraday-trend", action="store_true", help="Use H1+H4 trend alignment (ignores Daily) for active intraday trading")
    autotrade_parser.add_argument(
        "--trend-tf", "--trend-timeframes",
        dest="trend_timeframes",
        nargs="+",
        choices=("M15", "H1", "H4", "D"),
        default=None,
        help="Higher timeframes used for directional trend bias (e.g. --trend-tf M15 H1 or --trend-tf M15)",
    )
    autotrade_parser.add_argument("--state", type=Path, default=Path("autotrade_state.json"))
    autotrade_parser.add_argument("--count", type=int, default=300, help="Candles per timeframe (30–5000)")
    autotrade_parser.add_argument("--interval", type=float, default=30, help="Poll interval in seconds (at least 10)")
    add_options(autotrade_parser)

    args = parser.parse_args(argv)
    try:
        if args.command == "demo":
            from .demo import demo
            return demo(args.json, args.write_data)
        for pair in args.pairs:
            configuration(args, pair)  # Validate before contacting the provider.
        if args.command == "replay":
            return replay(args)
        if not isfinite(args.interval) or args.interval < 10:
            raise ValueError("Poll interval must be finite and at least 10 seconds")
        if not 30 <= args.count <= 5000:
            raise ValueError("Candle count must be between 30 and 5000")
        if args.command == "autotrade":
            oanda_token = os.environ.get("OANDA_API_TOKEN", "").strip()
            if "your_" in oanda_token.lower():
                oanda_token = ""

            paper_trader: PaperTrader | None = None
            bridge: MT5BridgeServer | None = None

            if args.broker == "paper":
                paper_trader = PaperTrader(initial_balance=args.initial_balance, risk_pct=args.risk_pct)
                broker = paper_trader
            elif args.broker == "oanda":
                if not oanda_token and sys.stdin.isatty():
                    oanda_token = getpass.getpass("OANDA API token: ").strip()
                if not oanda_token and not args.dry_run:
                    raise ValueError("OANDA_API_TOKEN is required when --broker oanda is selected")
                feed = OandaFeed(oanda_token, count=args.count) if oanda_token else None
                broker = OandaBroker(token=oanda_token, account_id=args.account_id or None, live=args.live) if oanda_token else None
            else:
                bridge = MT5BridgeServer(port=args.bridge_port)
                bridge.start()
                broker = MT5Broker(bridge)
                print(f"MT5 Bridge Server running at http://127.0.0.1:{args.bridge_port}")
                print("Attach SMC_Bridge.mq5 to your MetaTrader 5 chart.")

            # Select Feed
            if args.feed == "yahoo" or (not oanda_token and args.broker != "mt5" and args.feed != "mt5"):
                feed = YahooFeed()
            elif args.feed == "mt5" and bridge is not None:
                feed = MT5Feed(bridge)
            elif oanda_token:
                feed = OandaFeed(oanda_token, count=args.count)
            else:
                feed = YahooFeed()

            tg_token = (args.telegram_token or os.environ.get("TELEGRAM_BOT_TOKEN", "")).strip()
            tg_chat_id = (args.telegram_chat_id or os.environ.get("TELEGRAM_CHAT_ID", "")).strip()
            tg_client = TelegramClient(tg_token) if tg_token else None

            sweep_bars = args.sweep_bars
            entry_bars = args.entry_bars
            fx_gap = args.fx_gap
            if "M1" in args.timeframes:
                if args.sweep_bars == 12:
                    sweep_bars = 24
                if args.entry_bars == 12:
                    entry_bars = 24
                if args.fx_gap == 0.00005:
                    fx_gap = 0.00002

            return run_autotrader(
                broker=broker,
                feed=feed,
                telegram_client=tg_client,
                telegram_chat_id=tg_chat_id,
                pairs=args.pairs,
                timeframes=args.timeframes,
                state_path=str(args.state),
                risk_pct=args.risk_pct,
                interval=args.interval,
                swing_window=args.swing_window,
                reward_risk=args.rr,
                sweep_bars=sweep_bars,
                entry_bars=entry_bars,
                fx_buffer=args.fx_buffer,
                gold_buffer=args.gold_buffer,
                fx_gap=fx_gap,
                gold_gap=args.gold_gap,
                count=args.count,
                dry_run=args.dry_run,
                live=args.live,
                paper_trader=paper_trader,
                intraday_trend=args.intraday_trend,
                trend_timeframes=args.trend_timeframes,
            )
        return watch(args)
    except KeyboardInterrupt:
        status("Stopped.", getattr(args, "json", False))
        return 0
    except (ValueError, OSError, FeedError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
