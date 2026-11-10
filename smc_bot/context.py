"""Align higher-timeframe structure using only candles already closed."""

from bisect import bisect_right
from datetime import datetime
from functools import lru_cache

from .models import Candle, TREND_TIMEFRAMES, duration
from .strategy import structure_bias


class AlignedBias:
    def __init__(
        self,
        bundle: dict[str, list[Candle]],
        window: int = 2,
        trend_timeframes: tuple[str, ...] = TREND_TIMEFRAMES,
    ):
        self.trend_timeframes = trend_timeframes
        self.histories = {tf: bundle[tf] for tf in self.trend_timeframes}
        self.times = {tf: [c.time for c in bars] for tf, bars in self.histories.items()}
        self.window = window
        for tf, times in self.times.items():
            if any(a >= b for a, b in zip(times, times[1:])):
                raise ValueError(f"{tf}: candles must have unique, increasing close times")

    @lru_cache(maxsize=4096)
    def _structure(self, tf: str, count: int) -> str:
        return structure_bias(self.histories[tf][:count], self.window)

    def values_at(self, time: datetime) -> dict[str, str]:
        values = {}
        for tf in self.trend_timeframes:
            count = bisect_right(self.times[tf], time)
            if not count or time - self.times[tf][count - 1] > 3 * duration(tf):
                values[tf] = "neutral"
            else:
                values[tf] = self._structure(tf, count)
        return values

    def __call__(self, time: datetime) -> str:
        values = set(self.values_at(time).values())
        return values.pop() if len(values) == 1 else "neutral"
