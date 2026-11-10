"""Hierarchical Multi-Timeframe Regime and Order Flow Analyzer.

Evaluates Macro structure (Daily / 4H) against Intermediate structure (1H / 15m)
to determine whether the market is in an impulsive Pro-Trend Expansion or a
Counter-Trend Retracement back into discount/premium.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from typing import Any, Literal

from .models import Candle, duration
from .strategy import _pivot, structure_bias


@dataclass(frozen=True)
class MarketRegime:
    """Represents the multi-timeframe state at a given point in time."""

    time: datetime
    macro_bias: str          # "bullish" | "bearish" | "neutral"
    intermediate_bias: str   # "bullish" | "bearish" | "neutral"
    phase: str               # "EXPANSION" | "RETRACEMENT" | "CONSOLIDATION"
    active_side: str         # "buy" | "sell" | "both" | "none"
    classification: str      # "PRO_TREND" | "RETRACEMENT" | "INTRADAY_SWING" | "NONE"
    risk_factor: float       # 1.0 for pro-trend / intraday, 0.5 for retracement
    poi_target: float | None = None
    macro_range: tuple[float, float] | None = None  # (low, high)
    equilibrium: float | None = None


class HierarchicalRegimeDetector:
    """Detects macro narrative and intermediate phase using closed historical bars."""

    def __init__(
        self,
        bundle: dict[str, list[Candle]],
        macro_timeframes: tuple[str, ...] = ("H4", "D"),
        intermediate_timeframes: tuple[str, ...] = ("M15", "H1"),
        swing_window: int = 2,
    ):
        self.bundle = bundle
        self.macro_timeframes = macro_timeframes
        self.intermediate_timeframes = intermediate_timeframes
        self.swing_window = swing_window

        all_tfs = set(macro_timeframes) | set(intermediate_timeframes)
        self.times = {tf: [c.time for c in bundle.get(tf, [])] for tf in all_tfs}
        for tf, times in self.times.items():
            if not times:
                raise ValueError(f"HierarchicalRegimeDetector requires candles for {tf}")

    @lru_cache(maxsize=2048)
    def _bias_of(self, tf: str, count: int) -> str:
        candles = self.bundle[tf][:count]
        return structure_bias(candles, self.swing_window)

    def _get_tf_bias(self, tf: str, time: datetime) -> str:
        count = bisect_right(self.times[tf], time)
        if not count or time - self.times[tf][count - 1] > 3 * duration(tf):
            return "neutral"
        return self._bias_of(tf, count)

    def _combined_bias(self, timeframes: tuple[str, ...], time: datetime) -> str:
        biases = {self._get_tf_bias(tf, time) for tf in timeframes}
        if biases == {"bullish"}:
            return "bullish"
        if biases == {"bearish"}:
            return "bearish"
        # If one is trending and others are neutral, follow the trending timeframe
        trending = biases - {"neutral"}
        if len(trending) == 1:
            return trending.pop()
        return "neutral"

    def _find_equilibrium_and_poi(
        self, time: datetime, macro_side: str
    ) -> tuple[float | None, float | None, tuple[float, float] | None]:
        """Find the higher-timeframe swing high, low, and 50% equilibrium level."""
        # Use 4H or H1 to find recent swing range
        ref_tf = "H4" if "H4" in self.bundle and self.bundle["H4"] else "H1"
        count = bisect_right(self.times[ref_tf], time)
        if count < 10:
            return None, None, None

        candles = self.bundle[ref_tf][max(0, count - 30):count]
        highs = [c.high for c in candles]
        lows = [c.low for c in candles]
        swing_high = max(highs)
        swing_low = min(lows)
        if swing_high <= swing_low:
            return None, None, None

        eq = round((swing_high + swing_low) / 2.0, 5)
        # POI target for retracement is equilibrium or nearest unmitigated gap
        poi = eq
        return eq, poi, (swing_low, swing_high)

    def evaluate_at(self, time: datetime) -> MarketRegime:
        macro = self._combined_bias(self.macro_timeframes, time)
        intermediate = self._combined_bias(self.intermediate_timeframes, time)

        eq, poi, macro_range = self._find_equilibrium_and_poi(time, macro)

        # 1. Pro-Trend Expansion (Macro and Intermediate agree)
        if macro in ("bullish", "bearish") and intermediate == macro:
            return MarketRegime(
                time=time,
                macro_bias=macro,
                intermediate_bias=intermediate,
                phase="EXPANSION",
                active_side="buy" if macro == "bullish" else "sell",
                classification="PRO_TREND",
                risk_factor=1.0,
                poi_target=None,
                macro_range=macro_range,
                equilibrium=eq,
            )

        # 2. Counter-Trend Retracement (Macro is trending, but Intermediate is pulling back)
        if macro == "bullish" and intermediate == "bearish":
            return MarketRegime(
                time=time,
                macro_bias=macro,
                intermediate_bias=intermediate,
                phase="RETRACEMENT",
                active_side="sell",
                classification="RETRACEMENT",
                risk_factor=0.5,
                poi_target=poi,
                macro_range=macro_range,
                equilibrium=eq,
            )

        if macro == "bearish" and intermediate == "bullish":
            return MarketRegime(
                time=time,
                macro_bias=macro,
                intermediate_bias=intermediate,
                phase="RETRACEMENT",
                active_side="buy",
                classification="RETRACEMENT",
                risk_factor=0.5,
                poi_target=poi,
                macro_range=macro_range,
                equilibrium=eq,
            )

        # 3. Intermediate trending while Macro is Neutral (Intraday Swing)
        if macro == "neutral" and intermediate in ("bullish", "bearish"):
            return MarketRegime(
                time=time,
                macro_bias=macro,
                intermediate_bias=intermediate,
                phase="EXPANSION",
                active_side="buy" if intermediate == "bullish" else "sell",
                classification="INTRADAY_SWING",
                risk_factor=1.0,
                poi_target=None,
                macro_range=macro_range,
                equilibrium=eq,
            )

        # 4. Both Neutral / Consolidating
        return MarketRegime(
            time=time,
            macro_bias=macro,
            intermediate_bias=intermediate,
            phase="CONSOLIDATION",
            active_side="none",
            classification="NONE",
            risk_factor=0.0,
            poi_target=None,
            macro_range=macro_range,
            equilibrium=eq,
        )

