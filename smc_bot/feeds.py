"""Read completed candles from local CSV files.

CSV timestamps represent candle *close* times and must include a timezone.
Files must contain completed candles only; this loader cannot infer whether a
provider has written an unfinished candle. Prices use the provider's quote
units. A bundle lives at ``directory/PAIR/TIMEFRAME.csv``.
"""

import csv
import json
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request

from .models import Candle, PAIRS, TIMEFRAMES, TREND_TIMEFRAMES, duration


CSV_FIELDS = ("time", "open", "high", "low", "close")
ENTRY_TIMEFRAMES = ("M1", "M5", "M15")


class FeedError(RuntimeError):
    """A safe provider error message that does not expose credentials."""


def _bundle_frames(
    pair: str,
    entry_timeframes: tuple[str, ...],
    trend_timeframes: tuple[str, ...] | None = None,
) -> dict[str, None]:
    if pair not in PAIRS:
        raise ValueError(f"Unsupported pair {pair!r}; choose from {', '.join(PAIRS)}")
    if not entry_timeframes or any(frame not in ENTRY_TIMEFRAMES for frame in entry_timeframes):
        raise ValueError("Entry timeframes must contain M1, M5 and/or M15")
    trends = trend_timeframes if trend_timeframes is not None else TREND_TIMEFRAMES
    return dict.fromkeys((*entry_timeframes, *trends))


class OandaFeed:
    """Read midpoint candles from OANDA's practice API; never place orders.

    All intraday and daily candle boundaries use UTC. The API provides candle
    start times; this adapter adds the granularity duration so the strategy
    receives close times. Incomplete candles are discarded.
    """

    BASE_URL = "https://api-fxpractice.oanda.com"
    MIN_REQUEST_INTERVAL = 0.55

    def __init__(self, token: str, count: int = 300):
        if not isinstance(token, str) or not token.strip():
            raise ValueError("OANDA_API_TOKEN is required for the practice feed")
        if any(character.isspace() for character in token.strip()):
            raise ValueError("OANDA_API_TOKEN must not contain whitespace")
        if isinstance(count, bool) or not isinstance(count, int) or not 30 <= count <= 5000:
            raise ValueError("OANDA candle count must be an integer between 30 and 5000")
        self._token = token.strip()
        self.count = count
        self._last_request_started: float | None = None
        self._request_lock = threading.Lock()

    def _pace(self) -> None:
        # Limit connection starts across every pair and timeframe on this feed.
        with self._request_lock:
            now = time.monotonic()
            if self._last_request_started is not None:
                delay = self.MIN_REQUEST_INTERVAL - (now - self._last_request_started)
                if delay > 0:
                    time.sleep(delay)
            self._last_request_started = time.monotonic()

    def candles(self, pair: str, timeframe: str) -> list[Candle]:
        if pair not in PAIRS:
            raise ValueError(f"Unsupported pair {pair!r}; choose from {', '.join(PAIRS)}")
        if timeframe not in TIMEFRAMES:
            raise ValueError(f"Unsupported timeframe {timeframe!r}")
        query = urllib.parse.urlencode({
            "granularity": timeframe,
            "count": self.count,
            "price": "M",
            "dailyAlignment": 0,
            "alignmentTimezone": "UTC",
        })
        request = urllib.request.Request(
            f"{self.BASE_URL}/v3/instruments/{pair}/candles?{query}",
            headers={"Authorization": f"Bearer {self._token}", "Accept": "application/json"},
            method="GET",
        )
        self._pace()
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            exc.close()
            explanation = {
                401: "check the OANDA_API_TOKEN for your practice account",
                403: "your practice account cannot access this instrument",
                404: "the instrument may be unavailable for your practice account",
                429: "the practice API rate limit was reached; try again later",
            }.get(exc.code, "the practice API rejected the request")
            raise FeedError(f"OANDA {pair}/{timeframe}: HTTP {exc.code}; {explanation}") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise FeedError(f"OANDA {pair}/{timeframe}: connection failed or timed out") from None
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise FeedError(f"OANDA {pair}/{timeframe}: response was not valid JSON") from None
        if not isinstance(payload, dict) or not isinstance(payload.get("candles"), list):
            raise FeedError(f"OANDA {pair}/{timeframe}: response has no candle list")

        candles: list[Candle] = []
        for index, row in enumerate(payload["candles"], start=1):
            if not isinstance(row, dict):
                raise FeedError(f"OANDA {pair}/{timeframe}: invalid candle {index}")
            if row.get("complete") is not True:
                continue
            try:
                raw_timestamp = row["time"]
                # OANDA commonly supplies nine fractional digits; keep six.
                normalized = re.sub(r"(\.\d{6})\d+(?=Z|[+-]\d{2}:\d{2}$)", r"\1", raw_timestamp)
                start = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
                if start.tzinfo is None or start.utcoffset() is None:
                    raise ValueError("candle timestamp must include a timezone")
                mid = row["mid"]
                candle = Candle(
                    time=start.astimezone(timezone.utc) + duration(timeframe),
                    open=float(mid["o"]), high=float(mid["h"]),
                    low=float(mid["l"]), close=float(mid["c"]),
                )
                if candles and candle.time <= candles[-1].time:
                    raise ValueError("candle close times must be strictly increasing and unique")
            except (KeyError, ValueError, TypeError, OverflowError):
                raise FeedError(f"OANDA {pair}/{timeframe}: invalid candle {index}") from None
            candles.append(candle)
        if not candles:
            raise FeedError(f"OANDA {pair}/{timeframe}: no completed candles returned")
        return candles

    def bundle(
        self,
        pair: str,
        entry_timeframes: tuple[str, ...],
        trend_timeframes: tuple[str, ...] | None = None,
    ) -> dict[str, list[Candle]]:
        return {frame: self.candles(pair, frame) for frame in _bundle_frames(pair, entry_timeframes, trend_timeframes)}


class YahooFeed:
    """Free real-time market data feed from Yahoo Finance with session cookies, crumbs, and caching.

    Requires no API keys, accounts, or authentication.
    Supplies EUR/USD, GBP/USD, and XAU/USD completed candles.
    """

    COOKIE_URL = "https://fc.yahoo.com"
    CRUMB_URL = "https://query2.finance.yahoo.com/v1/test/getcrumb"
    BASE_URLS = [
        "https://query2.finance.yahoo.com/v8/finance/chart",
        "https://query1.finance.yahoo.com/v8/finance/chart",
    ]
    SYMBOL_MAP = {
        "EUR_USD": "EURUSD=X",
        "GBP_USD": "GBPUSD=X",
        "XAU_USD": "GC=F",
    }
    TIMEFRAME_MAP = {
        "M1": ("1m", "5d"),
        "M5": ("5m", "5d"),
        "M15": ("15m", "5d"),
        "H1": ("60m", "1mo"),
        "D": ("1d", "1y"),
    }

    def __init__(self, user_agent: str = "Mozilla/5.0"):
        self.user_agent = user_agent
        self._lock = threading.Lock()
        self._last_request_time: float | None = None
        self._cookie: str = ""
        self._crumb: str = ""
        self._session_time: float = 0.0
        self._cache: dict[str, tuple[float, list[Candle]]] = {}

    def _pace(self) -> None:
        with self._lock:
            now = time.monotonic()
            if self._last_request_time is not None:
                delay = 0.2 - (now - self._last_request_time)
                if delay > 0:
                    time.sleep(delay)
            self._last_request_time = time.monotonic()

    def _ensure_session(self) -> tuple[str, str]:
        with self._lock:
            now = time.monotonic()
            if self._cookie and self._crumb and (now - self._session_time) < 3600:
                return self._cookie, self._crumb

            cookie = ""
            req = urllib.request.Request(
                self.COOKIE_URL,
                headers={"User-Agent": self.user_agent, "Accept": "text/html,application/xhtml+xml"},
            )
            try:
                urllib.request.urlopen(req, timeout=10)
            except urllib.error.HTTPError as exc:
                cookie = exc.headers.get("Set-Cookie", "")
            except Exception:
                pass

            crumb = ""
            if cookie:
                crumb_req = urllib.request.Request(
                    self.CRUMB_URL,
                    headers={"User-Agent": self.user_agent, "Cookie": cookie, "Accept": "text/plain"},
                )
                try:
                    with urllib.request.urlopen(crumb_req, timeout=10) as resp:
                        crumb = resp.read().decode("utf-8").strip()
                except Exception:
                    pass

            self._cookie = cookie
            self._crumb = crumb
            self._session_time = time.monotonic()
            return self._cookie, self._crumb

    def _fetch_raw(self, symbol: str, interval: str, range_str: str) -> dict:
        cookie, crumb = self._ensure_session()
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/json",
        }
        if cookie:
            headers["Cookie"] = cookie

        last_error: Exception | None = None

        for base_url in self.BASE_URLS:
            for attempt in range(2):
                self._pace()
                url = f"{base_url}/{symbol}?interval={interval}&range={range_str}"
                if crumb:
                    url += f"&crumb={crumb}"
                req = urllib.request.Request(url, headers=headers)
                try:
                    with urllib.request.urlopen(req, timeout=12) as resp:
                        data = json.loads(resp.read().decode("utf-8"))
                        res = data.get("chart", {}).get("result")
                        if not res:
                            raise FeedError(f"Yahoo Finance returned empty chart for {symbol}")
                        return res[0]
                except urllib.error.HTTPError as exc:
                    exc.close()
                    last_error = exc
                    if exc.code in (401, 429):
                        with self._lock:
                            self._cookie = ""
                            self._crumb = ""
                        time.sleep(1.0)
                        cookie, crumb = self._ensure_session()
                        if cookie:
                            headers["Cookie"] = cookie
                except Exception as exc:
                    last_error = exc
                    time.sleep(0.5)

        raise FeedError(f"Yahoo Finance connection failed for {symbol}: {last_error}")

    def candles(self, pair: str, timeframe: str) -> list[Candle]:
        if pair not in PAIRS:
            raise ValueError(f"Unsupported pair {pair!r}")
        if timeframe not in TIMEFRAMES:
            raise ValueError(f"Unsupported timeframe {timeframe!r}")

        cache_key = f"{pair}:{timeframe}"
        now_mono = time.monotonic()
        # Cache Higher Timeframes to drastically reduce HTTP traffic:
        # D: 1800s (30m), H1: 300s (5m), H4: 300s (5m), M15: 30s
        ttl = {"D": 1800, "H1": 300, "H4": 300, "M15": 30}.get(timeframe, 0)
        if ttl > 0 and cache_key in self._cache:
            entry_time, cached = self._cache[cache_key]
            if (now_mono - entry_time) < ttl:
                return cached

        symbol = self.SYMBOL_MAP[pair]

        if timeframe == "H4":
            h1_candles = self.candles(pair, "H1")
            h4_candles = self._aggregate_h4(h1_candles)
            if ttl > 0:
                self._cache[cache_key] = (now_mono, h4_candles)
            return h4_candles

        interval, range_str = self.TIMEFRAME_MAP[timeframe]
        delta = duration(timeframe)
        raw = self._fetch_raw(symbol, interval, range_str)

        timestamps = raw.get("timestamp", [])
        quotes = raw.get("indicators", {}).get("quote", [{}])[0]
        opens = quotes.get("open", [])
        highs = quotes.get("high", [])
        lows = quotes.get("low", [])
        closes = quotes.get("close", [])

        candles: list[Candle] = []
        now = datetime.now(timezone.utc)

        for i in range(len(timestamps)):
            o, h, l, c = opens[i], highs[i], lows[i], closes[i]
            if None in (o, h, l, c):
                continue
            close_time = datetime.fromtimestamp(timestamps[i], tz=timezone.utc) + delta
            if close_time > now:
                continue

            try:
                candle = Candle(
                    time=close_time,
                    open=float(o),
                    high=float(h),
                    low=float(l),
                    close=float(c),
                )
                if candles and candle.time <= candles[-1].time:
                    continue
                candles.append(candle)
            except Exception:
                pass

        if not candles:
            raise FeedError(f"No completed candles returned for {pair} {timeframe}")

        if ttl > 0:
            self._cache[cache_key] = (now_mono, candles)
        return candles

    def _aggregate_h4(self, h1_candles: list[Candle]) -> list[Candle]:
        """Aggregate 1-hour candles into 4-hour candles aligned to 00, 04, 08, 12, 16, 20 UTC."""
        from collections import defaultdict
        buckets = defaultdict(list)

        for c in h1_candles:
            h = c.time.hour
            bucket_hour = ((h - 1) // 4 + 1) * 4
            if bucket_hour == 24:
                bucket_time = c.time.replace(hour=0, minute=0, second=0, microsecond=0) + duration("D")
            else:
                bucket_time = c.time.replace(hour=bucket_hour, minute=0, second=0, microsecond=0)
            buckets[bucket_time].append(c)

        h4_candles = []
        for bucket_time in sorted(buckets.keys()):
            bars = buckets[bucket_time]
            if not bars:
                continue
            h4_candles.append(
                Candle(
                    time=bucket_time,
                    open=bars[0].open,
                    high=max(b.high for b in bars),
                    low=min(b.low for b in bars),
                    close=bars[-1].close,
                )
            )
        return h4_candles

    def bundle(
        self,
        pair: str,
        entry_timeframes: tuple[str, ...],
        trend_timeframes: tuple[str, ...] | None = None,
    ) -> dict[str, list[Candle]]:
        return {frame: self.candles(pair, frame) for frame in _bundle_frames(pair, entry_timeframes, trend_timeframes)}



def load_csv(path: Path) -> list[Candle]:
    """Load and validate OHLC data, normalizing close timestamps to UTC.

    Invalid files raise ``ValueError`` with the filename and row number. I/O
    errors retain their usual exception types and include the filename.
    """
    path = Path(path)
    candles: list[Candle] = []
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != list(CSV_FIELDS):
            raise ValueError(
                f"{path}: row 1: expected CSV header {','.join(CSV_FIELDS)}"
            )
        for row in reader:
            row_number = reader.line_num
            try:
                if None in row or any(row[field] is None for field in CSV_FIELDS):
                    raise ValueError("expected exactly five CSV columns")
                timestamp = datetime.fromisoformat(row["time"].strip().replace("Z", "+00:00"))
                if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                    raise ValueError("candle close time must include a timezone")
                candle = Candle(
                    time=timestamp.astimezone(timezone.utc),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                )
                if candles and candle.time <= candles[-1].time:
                    raise ValueError("candle close times must be strictly increasing and unique")
            except (ValueError, TypeError) as exc:
                raise ValueError(f"{path}: row {row_number}: {exc}") from exc
            candles.append(candle)
    if not candles:
        raise ValueError(f"{path}: row 2: CSV contains no candles")
    return candles


def load_bundle(
    directory: Path, pair: str, entry_timeframes: tuple[str, ...]
) -> dict[str, list[Candle]]:
    """Load entry candles plus H1, H4, and daily trend candles for one pair."""
    frames = _bundle_frames(pair, entry_timeframes)
    return {frame: load_csv(Path(directory) / pair / f"{frame}.csv") for frame in frames}
