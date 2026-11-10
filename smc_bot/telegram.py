"""Telegram Bot Client and interactive command controller.

Enables instant mobile trade notifications and remote management via Telegram:
/status, /balance, /trades, /pause, /resume, /risk, /closeall, /help.
Uses Python standard library without third-party dependencies.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

logger = logging.getLogger("smc_bot.telegram")


class TelegramError(RuntimeError):
    """Telegram API error that conceals authentication tokens."""


class TelegramClient:
    """Lightweight Telegram Bot API client using standard library."""

    BASE_URL = "https://api.telegram.org"

    def __init__(self, token: str | None = None):
        raw_token = os.environ.get("TELEGRAM_BOT_TOKEN", "") if token is None else token
        if not isinstance(raw_token, str) or not raw_token.strip():
            raise ValueError("TELEGRAM_BOT_TOKEN is required")
        self._token = raw_token.strip()

    def _request(self, method: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.BASE_URL}/bot{self._token}/{method}"
        body = json.dumps(data).encode("utf-8") if data is not None else None
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=body, headers=headers, method="POST" if body else "GET")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = json.loads(resp.read())
                if not payload.get("ok"):
                    desc = payload.get("description", "Unknown Telegram error")
                    raise TelegramError(f"Telegram error: {desc}")
                return payload.get("result", {})
        except urllib.error.HTTPError as exc:
            err_msg = f"HTTP {exc.code}"
            try:
                err_data = json.loads(exc.read())
                if "description" in err_data:
                    err_msg += f": {err_data['description']}"
            except Exception:
                pass
            exc.close()
            raise TelegramError(f"Telegram API request failed: {err_msg}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TelegramError(f"Telegram connection error: {exc}") from None
        except json.JSONDecodeError:
            raise TelegramError("Telegram returned non-JSON response") from None

    def send_message(
        self,
        chat_id: str | int,
        text: str,
        parse_mode: str = "HTML",
        disable_web_page_preview: bool = True,
    ) -> dict[str, Any]:
        """Send a message to a Telegram chat or channel."""
        data = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": disable_web_page_preview,
        }
        return self._request("sendMessage", data)

    def get_updates(self, offset: int = 0, timeout: int = 10) -> list[dict[str, Any]]:
        """Long-poll Telegram for new messages."""
        data = {"offset": offset, "timeout": timeout}
        res = self._request("getUpdates", data)
        return res if isinstance(res, list) else []


class TelegramController:
    """Background listener for user commands sent to the Telegram bot."""

    def __init__(
        self,
        client: TelegramClient,
        allowed_chat_ids: set[str | int] | None = None,
        autotrader: Any = None,
    ):
        self.client = client
        self.allowed_chat_ids = allowed_chat_ids or set()
        self.autotrader = autotrader
        self._running = False
        self._thread: threading.Thread | None = None
        self._offset = 0

    def start(self) -> None:
        """Start polling in a background daemon thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True, name="TelegramListener")
        self._thread.start()

    def stop(self) -> None:
        """Stop polling thread."""
        self._running = False

    def _is_authorized(self, chat_id: str | int) -> bool:
        if not self.allowed_chat_ids:
            return True
        return str(chat_id) in {str(cid) for cid in self.allowed_chat_ids}

    def _poll_loop(self) -> None:
        while self._running:
            try:
                updates = self.client.get_updates(offset=self._offset, timeout=5)
                for update in updates:
                    up_id = update.get("update_id", 0)
                    if up_id >= self._offset:
                        self._offset = up_id + 1
                    msg = update.get("message")
                    if msg:
                        self._handle_message(msg)
            except Exception as exc:
                time.sleep(2)

    def _handle_message(self, msg: dict[str, Any]) -> None:
        chat = msg.get("chat", {})
        chat_id = chat.get("id")
        text = (msg.get("text") or "").strip()
        if not chat_id or not text:
            return

        if not self._is_authorized(chat_id):
            self.client.send_message(
                chat_id,
                "⚠️ <b>Access Denied:</b> This bot is private. Your chat ID is not authorized.",
            )
            return

        command_parts = text.split()
        cmd = command_parts[0].lower().split("@")[0]  # strip bot username if present
        args = command_parts[1:]

        try:
            if cmd in ("/start", "/help"):
                self._cmd_help(chat_id)
            elif cmd in ("/stats", "/performance"):
                self._cmd_performance(chat_id)
            elif cmd == "/status":
                self._cmd_status(chat_id)
            elif cmd == "/balance":
                self._cmd_balance(chat_id)
            elif cmd == "/trades":
                self._cmd_trades(chat_id)
            elif cmd == "/pause":
                self._cmd_pause(chat_id)
            elif cmd == "/resume":
                self._cmd_resume(chat_id)
            elif cmd == "/risk":
                self._cmd_risk(chat_id, args)
            elif cmd == "/closeall":
                self._cmd_closeall(chat_id)
            elif cmd == "/reset":
                self._cmd_reset(chat_id, args)
            else:
                self.client.send_message(
                    chat_id,
                    f"❓ Unknown command: <code>{text}</code>. Type /help for options.",
                )
        except Exception as exc:
            self.client.send_message(chat_id, f"❌ Error processing command: {exc}")

    def _cmd_help(self, chat_id: str | int) -> None:
        msg = (
            "🤖 <b>Forex SMC Trading Assistant</b>\n\n"
            "<b>Available Commands:</b>\n"
            "📊 /status - Bot state, execution mode & watched pairs\n"
            "📈 /stats - Performance dashboard (Win Rate, Profit Factor, Drawdown, Total R)\n"
            "💰 /balance - Account NAV, balance, and P/L\n"
            "📋 /trades - View active positions and pending limit orders\n"
            "⏸️ /pause - Pause automated execution (alerts only)\n"
            "▶️ /resume - Resume automated execution\n"
            "⚙️ /risk &lt;pct&gt; - Update risk % per trade (e.g. <code>/risk 1.0</code>)\n"
            "🔄 /reset [amount] - Reset paper trading balance & scorecard\n"
            "🚨 /closeall - Emergency kill-switch (cancel all orders & close trades)\n"
            "ℹ️ /help - Show this command menu"
        )
        self.client.send_message(chat_id, msg)

    def _cmd_performance(self, chat_id: str | int) -> None:
        pt = getattr(self.autotrader, "paper_trader", None)
        if not pt:
            self.client.send_message(chat_id, "ℹ️ Paper trading metrics not active in this session.")
            return

        m = pt.get_metrics()
        ret_icon = "🟢" if m["return_pct"] >= 0 else "🔴"
        r_icon = "🟢" if m["total_r"] >= 0 else "🔴"

        msg = (
            "📊 <b>SMC Paper Trading Performance</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"• <b>Starting Capital:</b> ${m['initial_balance']:,.2f}\n"
            f"• <b>Current Balance:</b> ${m['current_balance']:,.2f} ({ret_icon} {m['return_pct']:+.2f}%)\n"
            f"• <b>Net Realized P/L:</b> ${m['net_pnl']:+,.2f}\n"
            f"• <b>Total Trades:</b> {m['total_trades']} ({m['wins']} Wins / {m['losses']} Losses)\n"
            f"• <b>Win Rate:</b> <b>{m['win_rate']:.1f}%</b>\n"
            f"• <b>Profit Factor:</b> <b>{m['profit_factor']:.2f}</b>\n"
            f"• <b>Total Realized R:</b> {r_icon} <b>{m['total_r']:+.1f}R</b>\n"
            f"• <b>Max Drawdown:</b> <b>{m['max_drawdown_pct']:.2f}%</b>\n"
            f"• <b>Expectancy:</b> {m['expectancy_r']:+.2f}R / trade\n"
            f"• <b>Open Trades:</b> {m['open_positions']} | <b>Pending:</b> {m['pending_orders']}\n"
        )
        self.client.send_message(chat_id, msg)

    def _cmd_status(self, chat_id: str | int) -> None:
        if not self.autotrader:
            self.client.send_message(chat_id, "ℹ️ Bot is active in alert mode.")
            return

        paused = getattr(self.autotrader, "paused", False)
        mode = "⏸️ <b>PAUSED</b> (Alerts only)" if paused else "🟢 <b>ACTIVE</b> (Auto-Execution)"
        risk = getattr(self.autotrader, "risk_pct", 1.0)
        pairs = getattr(self.autotrader, "pairs", ["EUR_USD", "GBP_USD", "XAU_USD"])
        tfs = getattr(self.autotrader, "timeframes", ["M5", "M15"])
        trend_tfs = getattr(self.autotrader, "trend_timeframes", ["H1", "H4", "D"])
        engine = getattr(self.autotrader, "engine", "hierarchical").replace("_", " ").title()
        quality = getattr(self.autotrader, "quality", "high")
        quality_str = "💎 High Conviction (Premium/Discount Gate)" if quality == "high" else "Standard"

        msg = (
            f"🤖 <b>Bot Status</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"• <b>Engine:</b> {engine} (Expansion & Retracement)\n"
            f"• <b>Quality Filter:</b> {quality_str}\n"
            f"• <b>Execution:</b> {mode}\n"
            f"• <b>Risk per trade:</b> {risk:.2f}%\n"
            f"• <b>Pairs:</b> {', '.join(pairs)}\n"
            f"• <b>Timeframes:</b> {', '.join(tfs)}\n"
            f"• <b>Context Timeframes:</b> {', '.join(trend_tfs) if trend_tfs else 'Standalone'}\n"
            f"• <b>Live Mode:</b> {'Yes (Real Funds)' if getattr(self.autotrader, 'live', False) else 'No (Practice)'}\n"
        )
        self.client.send_message(chat_id, msg)

    def _cmd_balance(self, chat_id: str | int) -> None:
        if not self.autotrader or not getattr(self.autotrader, "broker", None):
            self.client.send_message(chat_id, "⚠️ No broker connected.")
            return

        summary = self.autotrader.broker.get_account_summary()
        currency = summary.get("currency", "USD")
        nav = float(summary.get("NAV", 0.0))
        balance = float(summary.get("balance", 0.0))
        upl = float(summary.get("unrealizedPL", 0.0))
        margin_used = float(summary.get("marginUsed", 0.0))
        margin_avail = float(summary.get("marginAvailable", 0.0))
        open_trades = summary.get("openTradeCount", 0)

        upl_emoji = "🟢" if upl >= 0 else "🔴"
        msg = (
            f"💰 <b>Account Summary</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"• <b>NAV:</b> ${nav:,.2f} {currency}\n"
            f"• <b>Balance:</b> ${balance:,.2f} {currency}\n"
            f"• <b>Unrealized P/L:</b> {upl_emoji} ${upl:+,.2f}\n"
            f"• <b>Open Trades:</b> {open_trades}\n"
            f"• <b>Margin Used:</b> ${margin_used:,.2f}\n"
            f"• <b>Margin Free:</b> ${margin_avail:,.2f}\n"
        )
        self.client.send_message(chat_id, msg)

    def _cmd_trades(self, chat_id: str | int) -> None:
        if not self.autotrader or not getattr(self.autotrader, "broker", None):
            self.client.send_message(chat_id, "⚠️ No broker connected.")
            return

        broker = self.autotrader.broker
        trades = broker.get_open_trades()
        orders = broker.get_pending_orders()

        lines = ["📊 <b>Active Positions & Orders</b>\n━━━━━━━━━━━━━━━━━━"]
        if not trades and not orders:
            lines.append("No open positions or pending orders.")
        else:
            if trades:
                lines.append(f"<b>Open Trades ({len(trades)}):</b>")
                for t in trades:
                    tid = t.get("id")
                    inst = t.get("instrument")
                    units = t.get("currentUnits")
                    side = "BUY" if float(units or 0) > 0 else "SELL"
                    price = t.get("price")
                    pl = float(t.get("unrealizedPL", 0.0))
                    pl_icon = "🟢" if pl >= 0 else "🔴"
                    lines.append(f"• #{tid} <b>{inst}</b> {side} {abs(float(units or 0)):.0f}u @ {price} | {pl_icon} ${pl:+,.2f}")
                lines.append("")

            if orders:
                lines.append(f"<b>Pending Orders ({len(orders)}):</b>")
                for o in orders:
                    oid = o.get("id")
                    inst = o.get("instrument")
                    typ = o.get("type")
                    units = o.get("units")
                    side = "BUY" if float(units or 0) > 0 else "SELL"
                    price = o.get("price")
                    lines.append(f"• #{oid} <b>{inst}</b> {typ} {side} {abs(float(units or 0)):.0f}u @ {price}")

        self.client.send_message(chat_id, "\n".join(lines))

    def _cmd_pause(self, chat_id: str | int) -> None:
        if self.autotrader:
            self.autotrader.paused = True
        self.client.send_message(
            chat_id,
            "⏸️ <b>Auto-trading PAUSED</b>.\nThe bot will continue scanning and sending alerts, but will NOT submit new orders.",
        )

    def _cmd_resume(self, chat_id: str | int) -> None:
        if self.autotrader:
            self.autotrader.paused = False
        self.client.send_message(
            chat_id,
            "▶️ <b>Auto-trading RESUMED</b>.\nThe bot will automatically execute limit orders when SMC setups occur.",
        )

    def _cmd_risk(self, chat_id: str | int, args: list[str]) -> None:
        if not args:
            cur = getattr(self.autotrader, "risk_pct", 1.0)
            self.client.send_message(chat_id, f"Current risk setting: <b>{cur:.2f}%</b> per trade.\nTo change, use <code>/risk 0.5</code> or <code>/risk 1.0</code>.")
            return
        try:
            val = float(args[0].replace("%", ""))
            if val <= 0 or val > 10:
                self.client.send_message(chat_id, "⚠️ Risk percentage must be between 0.1% and 10%.")
                return
            if self.autotrader:
                self.autotrader.risk_pct = val
            self.client.send_message(chat_id, f"✅ Risk per trade updated to <b>{val:.2f}%</b>.")
        except ValueError:
            self.client.send_message(chat_id, "⚠️ Invalid number format. Example: <code>/risk 1.0</code>")

    def _cmd_closeall(self, chat_id: str | int) -> None:
        if not self.autotrader or not getattr(self.autotrader, "broker", None):
            self.client.send_message(chat_id, "⚠️ No broker connected.")
            return

        self.client.send_message(chat_id, "🚨 <b>Triggering emergency kill-switch...</b>")
        results = self.autotrader.broker.close_all_trades()
        self.client.send_message(
            chat_id,
            f"✅ <b>Kill-switch completed:</b> {len(results)} actions taken (pending orders cancelled & positions closed).",
        )

    def _cmd_reset(self, chat_id: str | int, args: list[str]) -> None:
        pt = getattr(self.autotrader, "paper_trader", None)
        if not pt:
            self.client.send_message(chat_id, "ℹ️ Paper trading not active in this session.")
            return

        new_bal = None
        if args:
            try:
                new_bal = float(args[0])
            except ValueError:
                self.client.send_message(chat_id, "⚠️ Invalid balance amount. Usage: <code>/reset [amount]</code>")
                return

        pt.reset(new_bal)
        self.client.send_message(
            chat_id,
            f"🔄 <b>Paper Trading Scorecard Reset</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"• <b>New Balance:</b> ${pt.balance:,.2f}\n"
            f"• <b>Positions Cleared:</b> 0 open, 0 history\n"
            f"Ready for clean tracking."
        )

