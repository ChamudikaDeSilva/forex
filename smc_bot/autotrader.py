"""Automated trading orchestrator connecting the SMC signal scanner,

OANDA v20 execution, and the Telegram bot controller.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .broker import BrokerError, OandaBroker, format_price
from .context import AlignedBias
from .feeds import FeedError, OandaFeed
from .models import PAIRS, Config, Signal, config_for, duration
from .paper import PaperTrader
from .state import State
from .strategy import scan
from .telegram import TelegramClient, TelegramController

logger = logging.getLogger("smc_bot.autotrader")


class AutoTrader:
    """Coordinates candle scanning, automated order execution, and Telegram alerts."""

    def __init__(
        self,
        broker: OandaBroker | None = None,
        telegram_client: TelegramClient | None = None,
        telegram_chat_id: str | int | None = None,
        risk_pct: float = 1.0,
        paused: bool = False,
        dry_run: bool = False,
        pairs: list[str] | None = None,
        timeframes: list[str] | None = None,
        live: bool = False,
        verbose: bool = True,
        paper_trader: PaperTrader | None = None,
        trend_timeframes: list[str] | tuple[str, ...] | None = None,
    ):
        self.broker = broker
        self.telegram_client = telegram_client
        self.telegram_chat_id = telegram_chat_id or os.environ.get("TELEGRAM_CHAT_ID", "")
        self.risk_pct = risk_pct
        self.paused = paused
        self.dry_run = dry_run
        self.pairs = pairs or list(PAIRS)
        self.timeframes = timeframes or ["M5", "M15"]
        self.live = live
        self.verbose = verbose
        self.paper_trader = paper_trader
        self.trend_timeframes = list(trend_timeframes) if trend_timeframes else ["H1", "H4", "D"]
        self.active_orders: dict[str, str] = {}  # signal_key -> broker_order_id

    def send_alert(self, text: str) -> None:
        """Send message to Telegram if configured."""
        if self.telegram_client and self.telegram_chat_id:
            try:
                self.telegram_client.send_message(self.telegram_chat_id, text)
            except Exception as exc:
                print(f"[Telegram Error] {exc}", file=sys.stderr)

    def handle_signal(self, signal: Signal, pair: str, timeframe: str) -> None:
        """React to strategy signal: place/cancel orders and notify Telegram."""
        sig_key = f"{pair}:{timeframe}:{signal.setup_time.isoformat()}:{signal.side}"
        digits = 2 if pair == "XAU_USD" else 5

        if signal.kind == "setup":
            order_status = "⚠️ Execution Paused"
            units_text = "N/A"
            risk_usd = 0.0

            if not self.paused and self.broker:
                try:
                    summary = self.broker.get_account_summary()
                    nav = float(summary.get("NAV", summary.get("balance", 0.0)))
                    risk_usd = nav * (self.risk_pct / 100.0)
                    units = self.broker.calculate_units(
                        pair, signal.side, signal.entry, signal.stop, risk_pct=self.risk_pct
                    )
                    if isinstance(units, float):
                        units_text = f"{units:.2f} lots"
                    else:
                        units_text = f"{abs(units):,} units ({abs(units)/100000:.2f} lots)" if pair != "XAU_USD" else f"{abs(units)} oz"

                    if self.dry_run:
                        order_status = "🧪 Simulated (Dry Run - No Order Sent)"
                    else:
                        resp = self.broker.create_limit_order(
                            pair=pair,
                            side=signal.side,
                            entry=signal.entry,
                            stop=signal.stop,
                            target=signal.target,
                            units=units,
                            client_tag=f"smc_{pair}_{timeframe}",
                        )
                        order_created = resp.get("orderCreateTransaction", {})
                        order_id = order_created.get("id") or resp.get("lastTransactionID", "Unknown")
                        self.active_orders[sig_key] = str(order_id)
                        order_status = f"✅ Limit Order #{order_id} Placed ({'Live' if self.live else 'Practice'})"

                except Exception as exc:
                    order_status = f"❌ Execution Failed: {exc}"
            elif self.paused:
                order_status = "⏸️ Auto-Trading Paused (Alert Only)"

            side_emoji = "🟢" if signal.side == "buy" else "🔴"
            action = f"{signal.side.upper()} LIMIT"
            msg = (
                f"🎯 <b>SMC SETUP: {pair} ({timeframe})</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"• <b>Action:</b> {side_emoji} <b>{action}</b>\n"
                f"• <b>Entry:</b> <code>{format_price(pair, signal.entry)}</code>\n"
                f"• <b>Stop Loss:</b> <code>{format_price(pair, signal.stop)}</code>\n"
                f"• <b>Take Profit:</b> <code>{format_price(pair, signal.target)}</code> (2.0R)\n"
                f"• <b>FVG Range:</b> {format_price(pair, signal.gap_low)} – {format_price(pair, signal.gap_high)}\n"
                f"• <b>Position Size:</b> {units_text}\n"
                f"• <b>Planned Risk:</b> ${risk_usd:,.2f} ({self.risk_pct:.2f}%)\n"
                f"• <b>Status:</b> {order_status}\n"
                f"• <b>Expiry:</b> ~{signal.expires.strftime('%H:%M UTC')}\n"
            )
            if self.verbose:
                print(f"[{signal.time.isoformat()}] {pair} {timeframe} SETUP -> {order_status}")
            if self.paper_trader:
                self.paper_trader.on_setup(signal, pair, timeframe)
            self.send_alert(msg)

        elif signal.kind == "retest":
            msg = (
                f"🔔 <b>SMC RETEST: {pair} ({timeframe})</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"Completed candle touched entry price <code>{format_price(pair, signal.entry)}</code>.\n"
                f"If order was pending, it was filled."
            )
            if self.verbose:
                print(f"[{signal.time.isoformat()}] {pair} {timeframe} RETEST")
            self.send_alert(msg)

        elif signal.kind in ("cancelled", "expired"):
            order_info = ""
            order_id = self.active_orders.pop(sig_key, None)
            if order_id and self.broker and not self.dry_run:
                try:
                    self.broker.cancel_order(order_id)
                    order_info = f"\n• <b>Order Action:</b> Cancelled pending order #{order_id}"
                except Exception:
                    order_info = f"\n• <b>Order Action:</b> Pending order #{order_id} cleared"

            icon = "❌" if signal.kind == "cancelled" else "⏳"
            header = "SETUP CANCELLED" if signal.kind == "cancelled" else "SETUP EXPIRED"
            msg = (
                f"{icon} <b>SMC {header}: {pair} ({timeframe})</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"• <b>Reason:</b> {signal.reason}"
                f"{order_info}"
            )
            if self.verbose:
                print(f"[{signal.time.isoformat()}] {pair} {timeframe} {signal.kind.upper()} -> {signal.reason}")
            if self.paper_trader:
                self.paper_trader.cancel_pending(pair, timeframe, signal.reason)
            self.send_alert(msg)


def run_autotrader(
    broker: OandaBroker | None,
    feed: OandaFeed,
    telegram_client: TelegramClient | None,
    telegram_chat_id: str | int | None,
    pairs: list[str],
    timeframes: list[str],
    state_path: str = "autotrade_state.json",
    risk_pct: float = 1.0,
    interval: float = 30.0,
    swing_window: int = 2,
    reward_risk: float = 2.0,
    sweep_bars: int = 12,
    entry_bars: int = 12,
    fx_buffer: float = 0.0001,
    gold_buffer: float = 0.1,
    fx_gap: float = 0.00005,
    gold_gap: float = 0.05,
    count: int = 300,
    dry_run: bool = False,
    live: bool = False,
    paper_trader: PaperTrader | None = None,
    intraday_trend: bool = False,
    trend_timeframes: list[str] | tuple[str, ...] | None = None,
) -> int:
    """Main execution loop for scanning and trading."""
    trend_frames: tuple[str, ...]
    if trend_timeframes:
        trend_frames = tuple(trend_timeframes)
    elif intraday_trend:
        trend_frames = ("H1", "H4")
    else:
        trend_frames = ("H1", "H4", "D")

    state = State(Path(state_path))
    trader = AutoTrader(
        broker=broker,
        telegram_client=telegram_client,
        telegram_chat_id=telegram_chat_id,
        risk_pct=risk_pct,
        dry_run=dry_run,
        pairs=pairs,
        timeframes=timeframes,
        live=live,
        paper_trader=paper_trader,
        trend_timeframes=trend_frames,
    )

    controller: TelegramController | None = None
    if telegram_client and telegram_chat_id:
        controller = TelegramController(
            client=telegram_client,
            allowed_chat_ids={telegram_chat_id},
            autotrader=trader,
        )
        controller.start()
        trader.send_alert(
            f"🚀 <b>Forex SMC Auto-Trader Started</b>\n"
            f"• <b>Pairs:</b> {', '.join(pairs)}\n"
            f"• <b>Timeframes:</b> {', '.join(timeframes)}\n"
            f"• <b>Trend Filter:</b> {', '.join(trend_frames)}\n"
            f"• <b>Risk:</b> {risk_pct:.2f}%\n"
            f"• <b>Mode:</b> {'Dry Run (Simulated)' if dry_run else ('LIVE' if live else 'Practice')}\n"
            f"Send /help to view controls."
        )

    print(f"AutoTrader active. Monitoring {', '.join(pairs)} on {', '.join(timeframes)}.")
    print(f"Trend alignment filter: {', '.join(trend_frames)}.")
    print("Press Ctrl+C to stop.")

    warmed_up: dict[str, bool] = {}

    try:
        while True:
            started = time.monotonic()
            for pair in pairs:
                try:
                    gold = pair == "XAU_USD"
                    cfg = config_for(
                        pair,
                        swing_window=swing_window,
                        reward_risk=reward_risk,
                        sweep_timeout=sweep_bars,
                        entry_timeout=entry_bars,
                        stop_buffer=gold_buffer if gold else fx_buffer,
                        min_gap=gold_gap if gold else fx_gap,
                    )
                    bundle = feed.bundle(pair, tuple(timeframes), trend_timeframes=trend_frames)
                    bias = AlignedBias(bundle, swing_window, trend_timeframes=trend_frames)

                    for tf in timeframes:
                        candles = bundle[tf]
                        if not candles:
                            continue
                        latest = candles[-1].time
                        cur_bias = bias(latest)
                        bias_info = " ".join(f"{k}={v}" for k, v in bias.values_at(latest).items())

                        stream_key = f"{pair}:{tf}"
                        if stream_key not in warmed_up:
                            warmed_up[stream_key] = True
                            print(f"[{pair}] {tf} Active | Trend: {cur_bias.upper()} ({bias_info}) | Watching close: {latest.strftime('%H:%M:%S UTC')}")

                        checkpoint = state.last(stream_key)
                        if checkpoint is not None and latest <= checkpoint:
                            continue

                        events = scan(candles, cfg, tf, bias)
                        now = datetime.now(timezone.utc)
                        if checkpoint is None:
                            # Initial stream warmup: mark current latest candle so only new closed bars trade
                            pass
                        else:
                            print(f"[{pair}] {tf} Bar closed @ {latest.strftime('%H:%M:%S UTC')} | Bias: {cur_bias.upper()} | Scanning...")
                            fresh = now - 2 * duration(tf)
                            for event in events:
                                if event.time > checkpoint and event.time >= fresh:
                                    trader.handle_signal(event, pair, tf)

                        if trader.paper_trader:
                            last_bar = candles[-1]
                            resolved = trader.paper_trader.on_candle(pair, last_bar.high, last_bar.low, last_bar.time)
                            for ev in resolved:
                                if ev.get("type") == "FILLED":
                                    pos = ev["position"]
                                    fill_msg = (
                                        f"📬 <b>SMC PAPER ORDER FILLED: {pos.pair} ({pos.timeframe})</b>\n"
                                        f"━━━━━━━━━━━━━━━━━━\n"
                                        f"• <b>Side:</b> {pos.side.upper()}\n"
                                        f"• <b>Fill Price:</b> <code>{format_price(pos.pair, pos.entry)}</code>\n"
                                        f"• <b>Target:</b> <code>{format_price(pos.pair, pos.target)}</code>\n"
                                        f"• <b>Stop:</b> <code>{format_price(pos.pair, pos.stop)}</code>\n"
                                        f"Position is now OPEN and actively tracking TP/SL."
                                    )
                                    trader.send_alert(fill_msg)
                                    print(f"[{pos.pair}] {pos.timeframe} PAPER ORDER FILLED @ {format_price(pos.pair, pos.entry)}")
                                elif ev.get("type") == "CLOSED":
                                    pos = ev["position"]
                                    res = ev["result"]
                                    icon = "🎯" if res == "WIN" else "🛑"
                                    msg = (
                                        f"{icon} <b>SMC PAPER TRADE RESOLVED: {pos.pair} ({pos.timeframe})</b>\n"
                                        f"━━━━━━━━━━━━━━━━━━\n"
                                        f"• <b>Outcome:</b> <b>{res}</b>\n"
                                        f"• <b>Realized:</b> {pos.realized_r:+.1f}R (${pos.realized_pnl:+,.2f})\n"
                                        f"• <b>Exit Price:</b> <code>{pos.close_price}</code>\n"
                                        f"• <b>Reason:</b> {pos.close_reason}\n"
                                        f"• <b>Paper Balance:</b> ${trader.paper_trader.balance:,.2f}\n\n"
                                        f"Type /stats to see overall win rate and profit factor."
                                    )
                                    trader.send_alert(msg)

                        state.advance(f"{pair}:{tf}", latest)

                except (ValueError, OSError, FeedError, BrokerError) as exc:
                    print(f"[{pair}] Error: {exc}", file=sys.stderr)

            elapsed = time.monotonic() - started
            time.sleep(max(1.0, interval - elapsed))

    except KeyboardInterrupt:
        print("\nStopping AutoTrader...")
        if controller:
            controller.stop()
        trader.send_alert("🛑 <b>Forex SMC Auto-Trader Stopped.</b>")
        return 0
