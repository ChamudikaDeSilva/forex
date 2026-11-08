"""Deterministic synthetic candles for an offline end-to-end demonstration."""

import csv
from datetime import datetime, timezone
from pathlib import Path

from .context import AlignedBias
from .models import PAIRS, TREND_TIMEFRAMES, Candle, config_for, duration
from .strategy import scan


START = datetime(2026, 1, 12, 0, 0, tzinfo=timezone.utc)


def synthetic_bundle(pair: str = "EUR_USD") -> dict[str, list[Candle]]:
    base, scale = (2500.0, 1.0) if pair == "XAU_USD" else (1.1, 0.0001)

    def price(value: float) -> float:
        return base + (value - 100) * scale

    pattern = [
        (100, 100.5, 99.5, 100),
        (100, 101, 99, 100),
        (100, 102, 98, 100),  # confirmed strict high and low after two more bars
        (100, 101, 99, 100),
        (100, 100.5, 99.5, 100),
        (99, 101, 97, 99.5),  # sweep and reclaim
        (100, 104, 99, 103),  # directional structure break
        (103, 104, 102, 103),  # third candle confirms the FVG
        (103, 104, 101.4, 102),  # subsequent midpoint retracement
    ]
    bundle = {tf: [Candle(START + (index + 1) * duration(tf), *(price(v) for v in row))
                   for index, row in enumerate(pattern)] for tf in ("M5", "M15")}
    # Repeated rising swings confirm higher highs and higher lows in all filters.
    levels = [100, 102, 104, 106, 104, 102, 100, 102, 104, 108, 110, 108, 106, 104,
              106, 108, 112, 114, 112, 110, 108, 112, 114, 116, 114, 112, 110, 112, 114]
    for tf in TREND_TIMEFRAMES:
        bundle[tf] = [Candle(START - (len(levels) - index - 1) * duration(tf),
                             price(level), price(level + 0.4), price(level - 0.4), price(level))
                      for index, level in enumerate(levels)]
    return bundle


def write_samples(directory: Path) -> None:
    paths = [directory / pair / f"{tf}.csv" for pair in PAIRS
             for tf in ("M5", "M15", *TREND_TIMEFRAMES)]
    if any(path.exists() for path in paths):
        raise ValueError("Sample files already exist; choose an empty --write-data directory")
    for pair in PAIRS:
        for tf, candles in synthetic_bundle(pair).items():
            path = directory / pair / f"{tf}.csv"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", newline="", encoding="utf-8") as file:
                writer = csv.writer(file)
                writer.writerow(("time", "open", "high", "low", "close"))
                for candle in candles:
                    writer.writerow((candle.time.isoformat(), candle.open, candle.high, candle.low, candle.close))


def demo(json_output: bool = False, write_data: Path | None = None) -> int:
    from .cli import emit, status

    status("SYNTHETIC DEMO — invented prices and dates; these are not market signals.", json_output)
    if write_data is not None:
        write_samples(write_data)
        status(f"Synthetic CSV files written to {write_data}", json_output)
    bundle = synthetic_bundle()
    bias = AlignedBias(bundle)
    status("Example H1/H4/D structural trends: " + str(bias.values_at(START)), json_output)
    for tf in ("M5", "M15"):
        for event in scan(bundle[tf], config_for("EUR_USD"), tf, bias):
            emit(event, "EUR_USD", tf, json_output)
    return 0
