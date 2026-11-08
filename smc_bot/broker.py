"""OANDA v20 broker client for automated trade execution and account management.

Provides balance inspection, dynamic position sizing based on risk percentage,
order placement (limit orders with attached SL/TP), trade management, and order cancellation.
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .models import PAIRS


class BrokerError(RuntimeError):
    """Safe broker error that suppresses authentication credentials."""


def format_price(pair: str, price: float) -> str:
    """Format price to instrument precision (2 decimals for Gold, 5 for FX)."""
    digits = 2 if pair == "XAU_USD" else 5
    return f"{price:.{digits}f}"


class OandaBroker:
    """Execute trades and manage accounts on OANDA v20 REST API."""

    PRACTICE_URL = "https://api-fxpractice.oanda.com"
    LIVE_URL = "https://api-fxtrade.oanda.com"
    MIN_REQUEST_INTERVAL = 0.55

    def __init__(
        self,
        token: str | None = None,
        account_id: str | None = None,
        live: bool = False,
    ):
        raw_token = token or os.environ.get("OANDA_API_TOKEN", "")
        if not isinstance(raw_token, str) or not raw_token.strip():
            raise ValueError("OANDA API token is required")
        if any(c.isspace() for c in raw_token.strip()):
            raise ValueError("OANDA API token must not contain whitespace")

        self._token = raw_token.strip()
        self.live = live
        self.base_url = self.LIVE_URL if live else self.PRACTICE_URL
        self._account_id = (account_id or os.environ.get("OANDA_ACCOUNT_ID", "")).strip() or None
        self._last_request_started: float | None = None
        self._lock = threading.Lock()

    @property
    def account_id(self) -> str:
        """Get or discover the target account ID."""
        if not self._account_id:
            accounts = self.list_accounts()
            if not accounts:
                raise BrokerError("No OANDA accounts found for this token")
            self._account_id = accounts[0]["id"]
        return self._account_id

    def _pace(self) -> None:
        with self._lock:
            now = time.monotonic()
            if self._last_request_started is not None:
                delay = self.MIN_REQUEST_INTERVAL - (now - self._last_request_started)
                if delay > 0:
                    time.sleep(delay)
            self._last_request_started = time.monotonic()

    def _request(
        self,
        path: str,
        method: str = "GET",
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        body = json.dumps(data).encode("utf-8") if data is not None else None
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        self._pace()
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                content = resp.read()
                return json.loads(content) if content else {}
        except urllib.error.HTTPError as exc:
            err_msg = f"HTTP {exc.code}"
            try:
                err_data = json.loads(exc.read())
                if "errorMessage" in err_data:
                    err_msg += f": {err_data['errorMessage']}"
            except Exception:
                pass
            exc.close()
            raise BrokerError(f"OANDA API error ({path}): {err_msg}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise BrokerError(f"OANDA connection error ({path}): {exc}") from None
        except json.JSONDecodeError:
            raise BrokerError(f"OANDA returned non-JSON response from {path}") from None

    def list_accounts(self) -> list[dict[str, Any]]:
        """List accounts accessible with this token."""
        payload = self._request("/v3/accounts")
        return payload.get("accounts", [])

    def get_account_summary(self) -> dict[str, Any]:
        """Fetch balance, NAV, margin and unrealized P/L."""
        acc_id = self.account_id
        payload = self._request(f"/v3/accounts/{acc_id}/summary")
        return payload.get("account", {})

    def calculate_units(
        self,
        pair: str,
        side: str,
        entry: float,
        stop: float,
        risk_pct: float = 1.0,
    ) -> int:
        """Calculate unit size based on account NAV and risk percentage.

        Risk amount = NAV * (risk_pct / 100).
        Distance = |entry - stop|.
        Units = floor(Risk amount / Distance).
        Positive for buy, negative for sell.
        """
        if pair not in PAIRS:
            raise ValueError(f"Unsupported pair {pair!r}")
        if side not in ("buy", "sell"):
            raise ValueError(f"Invalid side {side!r}; must be 'buy' or 'sell'")
        if not math.isfinite(risk_pct) or risk_pct <= 0 or risk_pct > 20:
            raise ValueError("risk_pct must be positive and at most 20%")

        precision = 2 if pair == "XAU_USD" else 5
        distance = round(abs(entry - stop), precision)
        if distance <= 0:
            raise ValueError("Stop loss distance must be positive")

        summary = self.get_account_summary()
        nav = float(summary.get("NAV", summary.get("balance", 0.0)))
        if nav <= 0:
            raise BrokerError(f"Account NAV is insufficient ({nav})")

        risk_amount = nav * (risk_pct / 100.0)
        # For EUR/USD, GBP/USD, and XAU/USD (quote currency is USD),
        # 1 unit price change of 1.0 = $1.00 USD.
        raw_units = int(round(risk_amount / distance))
        if raw_units < 1:
            raise BrokerError(
                f"Calculated size {raw_units} is below minimum 1 unit (risk: ${risk_amount:.2f}, distance: {distance:.5f})"
            )

        return raw_units if side == "buy" else -raw_units

    def create_limit_order(
        self,
        pair: str,
        side: str,
        entry: float,
        stop: float,
        target: float,
        units: int | None = None,
        risk_pct: float = 1.0,
        client_tag: str | None = None,
    ) -> dict[str, Any]:
        """Place a limit order with attached Stop Loss and Take Profit."""
        if pair not in PAIRS:
            raise ValueError(f"Unsupported pair {pair!r}")
        if units is None:
            units = self.calculate_units(pair, side, entry, stop, risk_pct=risk_pct)

        order_data: dict[str, Any] = {
            "type": "LIMIT",
            "instrument": pair,
            "units": str(units),
            "price": format_price(pair, entry),
            "timeInForce": "GTC",
            "stopLossOnFill": {
                "price": format_price(pair, stop),
                "timeInForce": "GTC",
            },
            "takeProfitOnFill": {
                "price": format_price(pair, target),
                "timeInForce": "GTC",
            },
        }

        if client_tag:
            order_data["clientExtensions"] = {
                "id": client_tag[:128],
                "tag": "smc_bot",
            }

        acc_id = self.account_id
        return self._request(
            f"/v3/accounts/{acc_id}/orders",
            method="POST",
            data={"order": order_data},
        )

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        """Cancel a pending order."""
        acc_id = self.account_id
        return self._request(
            f"/v3/accounts/{acc_id}/orders/{order_id}/cancel",
            method="PUT",
        )

    def get_pending_orders(self) -> list[dict[str, Any]]:
        """List all pending orders."""
        acc_id = self.account_id
        payload = self._request(f"/v3/accounts/{acc_id}/pendingOrders")
        return payload.get("orders", [])

    def get_open_trades(self) -> list[dict[str, Any]]:
        """List all currently open trades."""
        acc_id = self.account_id
        payload = self._request(f"/v3/accounts/{acc_id}/openTrades")
        return payload.get("trades", [])

    def close_trade(self, trade_id: str) -> dict[str, Any]:
        """Close an open trade by its trade ID."""
        acc_id = self.account_id
        return self._request(
            f"/v3/accounts/{acc_id}/trades/{trade_id}/close",
            method="PUT",
        )

    def close_all_trades(self) -> list[dict[str, Any]]:
        """Emergency kill-switch: cancel all pending orders and close all open trades."""
        results: list[dict[str, Any]] = []
        orders = self.get_pending_orders()
        for ord_info in orders:
            oid = ord_info.get("id")
            if oid:
                try:
                    res = self.cancel_order(str(oid))
                    results.append({"type": "order_cancel", "id": oid, "result": res})
                except Exception as exc:
                    results.append({"type": "order_cancel_error", "id": oid, "error": str(exc)})

        trades = self.get_open_trades()
        for trade in trades:
            tid = trade.get("id")
            if tid:
                try:
                    res = self.close_trade(str(tid))
                    results.append({"type": "trade_close", "id": tid, "result": res})
                except Exception as exc:
                    results.append({"type": "trade_close_error", "id": tid, "error": str(exc)})

        return results
