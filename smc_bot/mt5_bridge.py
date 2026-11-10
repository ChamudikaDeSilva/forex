"""MetaTrader 5 (MT5) Bridge Server and Broker Adapter.

Runs a local, zero-dependency HTTP server that connects to the SMC_Bridge Expert Advisor
running on MetaTrader 5 desktop. Handles bidirectional communication:
- MT5 pushes account metrics and completed candles to Python.
- Python queues automated SMC Limit Orders, SL/TP adjustments, cancellations, and kill-switch commands for MT5 to execute.
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from .models import Candle, PAIRS, duration

logger = logging.getLogger("smc_bot.mt5_bridge")


def normalize_symbol(symbol: str) -> str:
    """Normalize symbols between OANDA and MT5 formats (e.g. EUR_USD <-> EURUSD)."""
    clean = symbol.replace("/", "").replace("_", "").upper()
    if clean in ("EURUSD", "GBPUSD", "XAUUSD"):
        if clean == "EURUSD":
            return "EUR_USD"
        if clean == "GBPUSD":
            return "GBP_USD"
        if clean == "XAUUSD":
            return "XAU_USD"
    return symbol


def to_mt5_symbol(pair: str) -> str:
    """Convert pair like EUR_USD to MT5 symbol format EURUSD."""
    return pair.replace("_", "")


class MT5BridgeServer:
    """Local HTTP Server that exchanges data with the MetaTrader 5 Expert Advisor."""

    def __init__(self, host: str = "127.0.0.1", port: int = 5001):
        self.host = host
        self.port = port
        self.server: ThreadingHTTPServer | None = None
        self.server_thread: threading.Thread | None = None
        self._lock = threading.Lock()

        # Shared state
        self.account_info: dict[str, Any] = {
            "login": "Waiting for MT5...",
            "balance": 10000.0,
            "equity": 10000.0,
            "currency": "USD",
            "margin": 0.0,
            "margin_free": 10000.0,
            "connected": False,
        }
        self.open_trades: list[dict[str, Any]] = []
        self.pending_orders: list[dict[str, Any]] = []
        self.candles_by_pair_tf: dict[str, list[Candle]] = {}
        self.queued_commands: list[dict[str, Any]] = []
        self.on_signal_callback: Callable[[dict[str, Any]], None] | None = None

    def start(self) -> None:
        """Start bridge server on a background daemon thread."""
        if self.server:
            return

        bridge = self

        class BridgeHandler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                pass  # Suppress default console access log spam

            def do_GET(self) -> None:
                if self.path == "/health":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"status": "ok", "mt5_connected": bridge.account_info.get("connected", False)}).encode("utf-8"))
                    return
                self.send_response(404)
                self.end_headers()

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length) if length > 0 else b"{}"
                try:
                    data = json.loads(body.decode("utf-8"))
                except Exception:
                    self.send_response(400)
                    self.end_headers()
                    return

                if self.path == "/api/sync":
                    # Receive state from MT5 and return queued orders
                    commands = bridge._handle_mt5_sync(data)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"status": "ok", "commands": commands}).encode("utf-8"))
                    return

                if self.path == "/api/event":
                    bridge._handle_mt5_event(data)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"status":"ok"}')
                    return

                self.send_response(404)
                self.end_headers()

        self.server = ThreadingHTTPServer((self.host, self.port), BridgeHandler)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True, name="MT5BridgeServer")
        self.server_thread.start()
        logger.info(f"MT5 Bridge Server listening on http://{self.host}:{self.port}")

    def stop(self) -> None:
        if self.server:
            srv = self.server
            self.server = None
            threading.Thread(target=srv.shutdown, daemon=True).start()
            try:
                srv.server_close()
            except Exception:
                pass

    def _handle_mt5_sync(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        with self._lock:
            # Update account info
            if "account" in data:
                acc = data["account"]
                self.account_info = {
                    "login": str(acc.get("login", "")),
                    "balance": float(acc.get("balance", 0.0)),
                    "equity": float(acc.get("equity", 0.0)),
                    "currency": acc.get("currency", "USD"),
                    "margin": float(acc.get("margin", 0.0)),
                    "margin_free": float(acc.get("margin_free", 0.0)),
                    "connected": True,
                    "last_sync": time.time(),
                }

            if "trades" in data:
                self.open_trades = data["trades"]

            if "orders" in data:
                self.pending_orders = data["orders"]

            # Ingest candles if supplied by MT5
            if "candles" in data:
                for item in data["candles"]:
                    raw_symbol = item.get("symbol", "")
                    tf = item.get("timeframe", "")
                    pair = normalize_symbol(raw_symbol)
                    rates = item.get("rates", [])
                    if pair in PAIRS and rates:
                        candle_list: list[Candle] = []
                        for r in rates:
                            try:
                                t = datetime.fromtimestamp(r["time"], tz=timezone.utc)
                                candle_list.append(
                                    Candle(
                                        time=t,
                                        open=float(r["open"]),
                                        high=float(r["high"]),
                                        low=float(r["low"]),
                                        close=float(r["close"]),
                                    )
                                )
                            except Exception:
                                pass
                        if candle_list:
                            self.candles_by_pair_tf[f"{pair}:{tf}"] = sorted(candle_list, key=lambda c: c.time)

            # Pop all pending commands to send to MT5
            commands = list(self.queued_commands)
            self.queued_commands.clear()
            return commands

    def _handle_mt5_event(self, data: dict[str, Any]) -> None:
        if self.on_signal_callback:
            try:
                self.on_signal_callback(data)
            except Exception as exc:
                logger.error(f"Error in MT5 event callback: {exc}")

    def queue_command(self, cmd: dict[str, Any]) -> None:
        with self._lock:
            self.queued_commands.append(cmd)


class MT5Broker:
    """Broker interface adapter backed by the MT5 Bridge Server."""

    def __init__(self, bridge: MT5BridgeServer):
        self.bridge = bridge

    def get_account_summary(self) -> dict[str, Any]:
        info = self.bridge.account_info
        nav = info.get("equity", info.get("balance", 10000.0))
        balance = info.get("balance", 10000.0)
        upl = nav - balance
        return {
            "account_id": info.get("login", "MT5_Demo"),
            "NAV": nav,
            "balance": balance,
            "currency": info.get("currency", "USD"),
            "unrealizedPL": upl,
            "marginUsed": info.get("margin", 0.0),
            "marginAvailable": info.get("margin_free", nav),
            "openTradeCount": len(self.bridge.open_trades),
        }

    def calculate_units(
        self,
        pair: str,
        side: str,
        entry: float,
        stop: float,
        risk_pct: float = 1.0,
    ) -> float:
        """Calculate MT5 lot size (e.g. 0.01 to 10.0 lots) based on risk percentage."""
        precision = 2 if pair == "XAU_USD" else 5
        distance = round(abs(entry - stop), precision)
        if distance <= 0:
            raise ValueError("Stop loss distance must be positive")

        summary = self.get_account_summary()
        nav = float(summary.get("NAV", 10000.0))
        risk_usd = nav * (risk_pct / 100.0)

        # In MT5:
        # For EURUSD / GBPUSD: 1 standard lot = 100,000 units ($10 per pip / 0.00010 = $1.00 per 0.00001)
        # 1 lot price move of 1.0 = $100,000 USD
        # For XAUUSD: 1 standard lot = 100 oz. $1.00 price move = $100 per lot
        if pair == "XAU_USD":
            # Risk = Lots * 100 * distance -> Lots = Risk / (100 * distance)
            lots = risk_usd / (100.0 * distance)
        else:
            # Risk = Lots * 100,000 * distance -> Lots = Risk / (100,000 * distance)
            lots = risk_usd / (100000.0 * distance)

        # Round to 2 decimal places (standard broker lot step = 0.01)
        lots = max(0.01, round(lots, 2))
        return lots

    def create_limit_order(
        self,
        pair: str,
        side: str,
        entry: float,
        stop: float,
        target: float,
        units: float | None = None,
        risk_pct: float = 1.0,
        client_tag: str | None = None,
    ) -> dict[str, Any]:
        """Queue an MT5 Limit Order with SL & TP."""
        lots = units if units is not None else self.calculate_units(pair, side, entry, stop, risk_pct=risk_pct)
        symbol = to_mt5_symbol(pair)
        digits = 2 if pair == "XAU_USD" else 5

        cmd = {
            "action": "ORDER_LIMIT",
            "symbol": symbol,
            "type": "BUY_LIMIT" if side == "buy" else "SELL_LIMIT",
            "price": round(entry, digits),
            "sl": round(stop, digits),
            "tp": round(target, digits),
            "lots": round(lots, 2),
            "comment": (client_tag or "smc_bot")[:31],
            "timestamp": time.time(),
        }
        self.bridge.queue_command(cmd)
        return {"status": "queued", "orderCreateTransaction": {"id": f"mt5_{symbol}_{int(time.time())}"}}

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        """Queue order cancellation on MT5."""
        cmd = {
            "action": "ORDER_CANCEL",
            "order_id": order_id,
            "timestamp": time.time(),
        }
        self.bridge.queue_command(cmd)
        return {"status": "queued", "orderCancelTransaction": {"orderID": order_id}}

    def close_all_trades(self) -> list[dict[str, Any]]:
        """Queue kill-switch on MT5: close all open positions and cancel pending orders."""
        cmd = {
            "action": "CLOSE_ALL",
            "timestamp": time.time(),
        }
        self.bridge.queue_command(cmd)
        return [{"status": "queued", "action": "CLOSE_ALL"}]

    def get_pending_orders(self) -> list[dict[str, Any]]:
        return self.bridge.pending_orders

    def get_open_trades(self) -> list[dict[str, Any]]:
        return self.bridge.open_trades


class MT5Feed:
    """Read candles synced from MetaTrader 5 Expert Advisor."""

    def __init__(self, bridge: MT5BridgeServer):
        self.bridge = bridge

    def candles(self, pair: str, timeframe: str) -> list[Candle]:
        return self.bridge.candles_by_pair_tf.get(f"{pair}:{timeframe}", [])

    def bundle(
        self,
        pair: str,
        entry_timeframes: tuple[str, ...],
        trend_timeframes: tuple[str, ...] | None = None,
    ) -> dict[str, list[Candle]]:
        from .feeds import _bundle_frames
        frames = _bundle_frames(pair, entry_timeframes, trend_timeframes)
        return {frame: self.candles(pair, frame) for frame in frames}

