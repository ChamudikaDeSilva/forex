from dataclasses import dataclass
from datetime import datetime, timedelta
from math import isfinite


TIMEFRAMES = {"M1": 60, "M5": 300, "M15": 900, "H1": 3600, "H4": 14400, "D": 86400}
PAIRS = ("EUR_USD", "GBP_USD", "XAU_USD")
TREND_TIMEFRAMES = ("H1", "H4", "D")


@dataclass(frozen=True)
class Candle:
    """An OHLC candle; time is its close time, always timezone-aware."""

    time: datetime
    open: float
    high: float
    low: float
    close: float

    def __post_init__(self) -> None:
        if self.time.tzinfo is None or self.time.utcoffset() is None:
            raise ValueError("Candle close time must include a timezone")
        prices = (self.open, self.high, self.low, self.close)
        if not all(isfinite(p) and p > 0 for p in prices):
            raise ValueError("OHLC prices must be finite and positive")
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close):
            raise ValueError("OHLC prices fall outside the candle's high/low")
        if self.low > self.high:
            raise ValueError("Candle low exceeds high")


@dataclass(frozen=True)
class Config:
    swing_window: int = 2
    reward_risk: float = 2.0
    stop_buffer: float = 0.0001
    min_gap: float = 0.00005
    sweep_timeout: int = 12
    entry_timeout: int = 12

    def __post_init__(self) -> None:
        if self.swing_window < 1 or self.sweep_timeout < 1 or self.entry_timeout < 1:
            raise ValueError("Swing window and expiry bars must be positive integers")
        if not isfinite(self.reward_risk) or self.reward_risk <= 0:
            raise ValueError("Reward/risk must be positive and finite")
        if not isfinite(self.stop_buffer) or self.stop_buffer <= 0:
            raise ValueError("Stop buffer must be positive and finite")
        if not isfinite(self.min_gap) or self.min_gap <= 0:
            raise ValueError("Minimum FVG size must be positive and finite")


def config_for(pair: str, **overrides: object) -> Config:
    # Gold uses absolute dollar distances: $0.10 buffer and $0.05 minimum gap.
    # These are configurable price distances, not universal broker pip sizes.
    values = {"stop_buffer": 0.1, "min_gap": 0.05} if pair == "XAU_USD" else {}
    values.update(overrides)
    return Config(**values)


@dataclass(frozen=True)
class Signal:
    kind: str  # setup, retest, cancelled, expired
    time: datetime
    setup_time: datetime
    side: str  # buy, sell
    entry: float
    stop: float
    target: float
    gap_low: float
    gap_high: float
    expires: datetime
    reason: str
    classification: str = "PRO_TREND"
    risk_factor: float = 1.0
    poi_target: float | None = None

    def key(self, pair: str, timeframe: str) -> str:
        return f"{pair}:{timeframe}:{self.setup_time.isoformat()}:{self.side}:{self.kind}"


def duration(timeframe: str) -> timedelta:
    return timedelta(seconds=TIMEFRAMES[timeframe])
