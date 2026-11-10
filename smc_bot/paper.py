"""Paper trading simulation engine and performance metrics calculator.

Simulates execution, tracks limit order fills, stop losses, and profit targets,
and computes institutional trading metrics: Win Rate, Profit Factor, Max Drawdown,
Total R-multiples, and Expectancy without risking real capital.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path
from typing import Any

from .broker import format_price
from .models import Signal


@dataclass
class PaperPosition:
    id: str
    pair: str
    timeframe: str
    side: str  # "buy" or "sell"
    entry: float
    stop: float
    target: float
    units: float
    risk_usd: float
    reward_risk: float
    status: str  # "PENDING", "OPEN", "WIN", "LOSS", "CANCELLED", "EXPIRED"
    setup_time: str
    filled_time: str | None = None
    closed_time: str | None = None
    realized_r: float = 0.0
    realized_pnl: float = 0.0
    close_price: float | None = None
    close_reason: str = ""
    classification: str = "PRO_TREND"


class PaperTrader:
    """Zero-risk paper trading broker that tracks positions and performance."""

    def __init__(
        self,
        initial_balance: float = 10000.0,
        risk_pct: float = 1.0,
        state_file: str | Path = "paper_trades.json",
    ):
        self.initial_balance = initial_balance
        self.balance = initial_balance
        self.risk_pct = risk_pct
        self.state_file = Path(state_file)
        self.positions: list[PaperPosition] = []
        self._load()

    def _load(self) -> None:
        if not self.state_file.exists():
            return
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            self.balance = float(data.get("balance", self.initial_balance))
            self.positions = [PaperPosition(**p) for p in data.get("positions", [])]
        except Exception:
            pass

    def save(self) -> None:
        try:
            data = {
                "initial_balance": self.initial_balance,
                "balance": self.balance,
                "positions": [asdict(p) for p in self.positions],
            }
            self.state_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            pass

    def reset(self, new_balance: float | None = None) -> None:
        """Reset paper trading state to clean starting balance."""
        if new_balance is not None:
            self.initial_balance = new_balance
        self.balance = self.initial_balance
        self.positions = []
        self.save()

    def get_account_summary(self) -> dict[str, Any]:
        """Conform to Broker summary interface."""
        unrealized = 0.0
        open_count = 0
        for p in self.positions:
            if p.status == "OPEN":
                open_count += 1

        nav = self.balance + unrealized
        return {
            "account_id": "PAPER_TRADING",
            "NAV": nav,
            "balance": self.balance,
            "currency": "USD",
            "unrealizedPL": unrealized,
            "marginUsed": 0.0,
            "marginAvailable": nav,
            "openTradeCount": open_count,
        }

    def calculate_units(
        self,
        pair: str,
        side: str,
        entry: float,
        stop: float,
        risk_pct: float = 1.0,
    ) -> float:
        distance = round(abs(entry - stop), 2 if pair == "XAU_USD" else 5)
        if distance <= 0:
            raise ValueError("Stop loss distance must be positive")
        risk_usd = self.balance * (risk_pct / 100.0)
        if pair == "XAU_USD":
            lots = risk_usd / (100.0 * distance)
        else:
            lots = risk_usd / (100000.0 * distance)
        return max(0.01, round(lots, 2))

    def on_setup(self, signal: Signal, pair: str, timeframe: str) -> PaperPosition:
        """Place a virtual limit order when SMC setup appears."""
        pos_id = f"paper_{pair}_{timeframe}_{int(datetime.now(timezone.utc).timestamp())}"
        eff_risk_pct = self.risk_pct * getattr(signal, "risk_factor", 1.0)
        lots = self.calculate_units(pair, signal.side, signal.entry, signal.stop, eff_risk_pct)
        risk_usd = self.balance * (eff_risk_pct / 100.0)
        rr = round(abs(signal.target - signal.entry) / abs(signal.entry - signal.stop), 2)

        pos = PaperPosition(
            id=pos_id,
            pair=pair,
            timeframe=timeframe,
            side=signal.side,
            entry=signal.entry,
            stop=signal.stop,
            target=signal.target,
            units=lots,
            risk_usd=risk_usd,
            reward_risk=rr,
            status="PENDING",
            setup_time=signal.setup_time.isoformat(),
            classification=getattr(signal, "classification", "PRO_TREND"),
        )
        self.positions.append(pos)
        self.save()
        return pos

    def on_candle(self, pair: str, candle_high: float, candle_low: float, candle_time: datetime) -> list[dict[str, Any]]:
        """Evaluate open & pending paper orders against incoming price action."""
        events: list[dict[str, Any]] = []

        for p in self.positions:
            if p.pair != pair:
                continue

            buying = p.side == "buy"

            # 1. Check if pending order gets filled
            if p.status == "PENDING":
                touched_entry = candle_low <= p.entry if buying else candle_high >= p.entry
                if touched_entry:
                    p.status = "OPEN"
                    p.filled_time = candle_time.isoformat()
                    events.append({"type": "FILLED", "position": p})

            # 2. Check open position for TP or SL
            if p.status == "OPEN":
                hit_target = candle_high >= p.target if buying else candle_low <= p.target
                hit_stop = candle_low <= p.stop if buying else candle_high >= p.stop

                if hit_target:
                    p.status = "WIN"
                    p.realized_r = p.reward_risk
                    p.realized_pnl = p.risk_usd * p.reward_risk
                    p.close_price = p.target
                    p.closed_time = candle_time.isoformat()
                    p.close_reason = f"Target hit (+{p.reward_risk:.1f}R)"
                    self.balance += p.realized_pnl
                    events.append({"type": "CLOSED", "result": "WIN", "position": p})

                elif hit_stop:
                    p.status = "LOSS"
                    p.realized_r = -1.0
                    p.realized_pnl = -p.risk_usd
                    p.close_price = p.stop
                    p.closed_time = candle_time.isoformat()
                    p.close_reason = "Stop loss hit (-1.0R)"
                    self.balance += p.realized_pnl
                    events.append({"type": "CLOSED", "result": "LOSS", "position": p})

        if events:
            self.save()
        return events

    def cancel_pending(self, pair: str, timeframe: str, reason: str = "Setup cancelled") -> list[PaperPosition]:
        cancelled = []
        for p in self.positions:
            if p.pair == pair and p.timeframe == timeframe and p.status == "PENDING":
                p.status = "CANCELLED"
                p.close_reason = reason
                cancelled.append(p)
        if cancelled:
            self.save()
        return cancelled

    def get_metrics(self) -> dict[str, Any]:
        """Compute comprehensive performance statistics."""
        closed = [p for p in self.positions if p.status in ("WIN", "LOSS")]
        wins = [p for p in closed if p.status == "WIN"]
        losses = [p for p in closed if p.status == "LOSS"]

        total_trades = len(closed)
        win_rate = (len(wins) / total_trades * 100.0) if total_trades > 0 else 0.0

        gross_profit = sum(p.realized_pnl for p in wins)
        gross_loss = abs(sum(p.realized_pnl for p in losses))
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (gross_profit if gross_profit > 0 else 1.0)

        total_r = sum(p.realized_r for p in closed)
        net_pnl = sum(p.realized_pnl for p in closed)
        return_pct = ((self.balance - self.initial_balance) / self.initial_balance) * 100.0

        # Maximum Drawdown calculation
        peak = self.initial_balance
        max_dd = 0.0
        running_bal = self.initial_balance
        for p in closed:
            running_bal += p.realized_pnl
            if running_bal > peak:
                peak = running_bal
            dd = ((peak - running_bal) / peak) * 100.0 if peak > 0 else 0.0
            if dd > max_dd:
                max_dd = dd

        # Expectancy = (Win% * AvgWinR) - (Loss% * AvgLossR)
        avg_win_r = (sum(p.realized_r for p in wins) / len(wins)) if wins else 0.0
        loss_rate = (len(losses) / total_trades) if total_trades > 0 else 0.0
        win_rate_dec = (len(wins) / total_trades) if total_trades > 0 else 0.0
        expectancy_r = (win_rate_dec * avg_win_r) - (loss_rate * 1.0)

        open_positions = [p for p in self.positions if p.status == "OPEN"]
        pending_orders = [p for p in self.positions if p.status == "PENDING"]

        return {
            "initial_balance": self.initial_balance,
            "current_balance": self.balance,
            "return_pct": return_pct,
            "net_pnl": net_pnl,
            "total_trades": total_trades,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": win_rate,
            "profit_factor": profit_factor,
            "total_r": total_r,
            "max_drawdown_pct": max_dd,
            "expectancy_r": expectancy_r,
            "open_positions": len(open_positions),
            "pending_orders": len(pending_orders),
        }

