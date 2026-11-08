"""Deterministic SMC setup observations from completed OHLC candles.

Signals describe chart patterns, never an order submission or an executable fill.
Expiry counts subsequent supplied candles; ``Signal.expires`` is only the nominal
time estimate, so weekends and missing candles can extend the actual lifetime.
"""

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Callable

from .models import Candle, Config, Signal, duration


@dataclass(frozen=True)
class _Swing:
    index: int
    price: float


@dataclass(frozen=True)
class _Candidate:
    side: str
    sweep_index: int
    break_level: float
    stop: float
    break_index: int | None = None


def _validate_order(candles: list[Candle]) -> None:
    if any(current.time <= previous.time for previous, current in zip(candles, candles[1:])):
        raise ValueError("Candle close times must be strictly increasing and unique")


def _pivot(candles: list[Candle], index: int, window: int) -> tuple[_Swing | None, _Swing | None]:
    candle = candles[index]
    neighbors = candles[index - window:index] + candles[index + 1:index + window + 1]
    high = _Swing(index, candle.high) if all(candle.high > other.high for other in neighbors) else None
    low = _Swing(index, candle.low) if all(candle.low < other.low for other in neighbors) else None
    return high, low


def structure_bias(candles: list[Candle], window: int = 2) -> str:
    """HH + HL is bullish; LH + LL is bearish; other structures are neutral.

    Both pairs come from the latest two strictly confirmed swing highs/lows.
    A pivot needs ``window`` finished candles on each side to become available.
    """
    if not isinstance(window, int) or isinstance(window, bool) or window < 1:
        raise ValueError("Swing window must be a positive integer")
    _validate_order(candles)
    highs: list[float] = []
    lows: list[float] = []
    for index in range(window, len(candles) - window):
        high, low = _pivot(candles, index, window)
        if high is not None:
            highs.append(high.price)
        if low is not None:
            lows.append(low.price)
    if len(highs) < 2 or len(lows) < 2:
        return "neutral"
    if highs[-1] > highs[-2] and lows[-1] > lows[-2]:
        return "bullish"
    if highs[-1] < highs[-2] and lows[-1] < lows[-2]:
        return "bearish"
    return "neutral"


def scan(
    candles: list[Candle],
    config: Config,
    timeframe: str,
    bias_at: Callable[[datetime], str],
) -> list[Signal]:
    """Scan closed candles for sweep → structure break → FVG → retracement.

    ``bias_at(close_time)`` must use only H1/H4/D candles closed by that time;
    unanimous bullish/bearish alignment enables the corresponding direction.
    New pivots become usable on the candle after their confirmation candle.
    Structure must break on a later candle with a body in the setup direction.
    The break is the middle of the FVG's three candles. Entry monitoring begins
    after its third candle closes. A retest is a completed-candle observation.

    Stop touches and trend disagreement cancel a plan before retest detection.
    An opening gap beyond entry also cancels it. The Nth eligible candle can
    still retest; otherwise an expiry event is emitted at that candle's close.
    """
    _validate_order(candles)
    interval = duration(timeframe)
    window = config.swing_window
    if any(not isinstance(value, int) or isinstance(value, bool) for value in
           (window, config.sweep_timeout, config.entry_timeout)):
        raise ValueError("Swing window and expiry bars must be positive integers")

    highs: list[_Swing] = []
    lows: list[_Swing] = []
    consumed: set[tuple[str, int]] = set()
    candidate: _Candidate | None = None
    plan: Signal | None = None
    plan_index = -1
    events: list[Signal] = []

    def matches(side: str, bias: str) -> bool:
        return bias == ("bullish" if side == "buy" else "bearish")

    for index, candle in enumerate(candles):
        # This pivot was confirmed by the previous candle, before this one began.
        pivot_index = index - window - 1
        if pivot_index >= window:
            high, low = _pivot(candles, pivot_index, window)
            if high is not None:
                highs.append(high)
            if low is not None:
                lows.append(low)

        bias = bias_at(candle.time)
        if plan is not None:
            buying = plan.side == "buy"
            stop_touched = candle.low <= plan.stop if buying else candle.high >= plan.stop
            gap_through = candle.open < plan.entry if buying else candle.open > plan.entry
            touched_entry = candle.low <= plan.entry if buying else candle.high >= plan.entry
            if stop_touched or not matches(plan.side, bias) or gap_through:
                reason = ("Stop touched before a valid retracement observation" if stop_touched
                          else "H1/H4/D trend alignment no longer supports this setup"
                          if not matches(plan.side, bias)
                          else "Candle opened beyond entry; intrabar entry sequence is unknown")
                events.append(replace(plan, kind="cancelled", time=candle.time, reason=reason))
                plan = None
            elif touched_entry:
                events.append(replace(
                    plan, kind="retest", time=candle.time,
                    reason="Completed candle reached the planned entry; observed retest, not an order or fill",
                ))
                plan = None
            elif index - plan_index >= config.entry_timeout:
                events.append(replace(
                    plan, kind="expired", time=candle.time,
                    reason=f"No valid retracement in {config.entry_timeout} subsequent completed candles",
                ))
                plan = None
            continue

        if candidate is not None:
            buying = candidate.side == "buy"
            stop_touched = candle.low <= candidate.stop if buying else candle.high >= candidate.stop
            if stop_touched or not matches(candidate.side, bias):
                candidate = None
                continue

            if candidate.break_index is not None:
                # Exactly the next candle completes the FVG surrounding the break.
                middle = candidate.break_index
                gap_low = candles[middle - 1].high if buying else candle.high
                gap_high = candle.low if buying else candles[middle - 1].low
                if gap_high - gap_low >= config.min_gap:
                    entry = (gap_low + gap_high) / 2
                    risk = entry - candidate.stop if buying else candidate.stop - entry
                    if risk > 0:
                        target = entry + config.reward_risk * risk if buying else entry - config.reward_risk * risk
                        if target > 0:
                            plan = Signal(
                                kind="setup", time=candle.time, setup_time=candle.time,
                                side=candidate.side, entry=entry, stop=candidate.stop,
                                target=target, gap_low=gap_low, gap_high=gap_high,
                                expires=candle.time + interval * config.entry_timeout,
                                reason=("Liquidity sweep, later directional structure close, and confirmed FVG; "
                                        f"entry plan valid for the next {config.entry_timeout} completed candles; "
                                        "expiry timestamp is an estimate"),
                            )
                            plan_index = index
                            events.append(plan)
                candidate = None
                continue

            breaks = (candle.close > candidate.break_level and candle.close > candle.open) if buying else (
                candle.close < candidate.break_level and candle.close < candle.open)
            if index - candidate.sweep_index <= config.sweep_timeout and breaks:
                candidate = replace(candidate, break_index=index)
            elif index - candidate.sweep_index >= config.sweep_timeout:
                candidate = None
            continue

        if not highs or not lows:
            continue
        high, low = highs[-1], lows[-1]
        if (bias == "bullish" and ("buy", low.index) not in consumed
                and candle.low < low.price < candle.close < high.price):
            consumed.add(("buy", low.index))
            candidate = _Candidate("buy", index, high.price, candle.low - config.stop_buffer)
        elif (bias == "bearish" and ("sell", high.index) not in consumed
              and candle.high > high.price > candle.close > low.price):
            consumed.add(("sell", high.index))
            candidate = _Candidate("sell", index, low.price, candle.high + config.stop_buffer)

    return events
