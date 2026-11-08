# SMC forex entry bot

A small Python console bot for **EUR/USD, GBP/USD, and XAU/USD (gold)**. It watches **M5 and M15** entries and requires **H1, H4, and daily** structural trends to agree. It reads the **OANDA practice API** and can replay CSV candles offline. Python **3.10+** is required; there are no third-party packages to install.

This is an initial signal prototype. It produces planned chart levels and closed-candle observations. Its trading performance has not been established. Spread, commission, slippage, and executable bid/ask prices are not modeled.

## Run it

From this project directory, first try the synthetic example:

```sh
python3 -m smc_bot demo
```

Start the practice-data watcher:

```sh
python3 -m smc_bot watch
```

The terminal prompts for your OANDA practice API token without displaying it. Alternatively, set the `OANDA_API_TOKEN` environment variable before starting. Tokens are kept in memory and are never written to the state file. OANDA's [authentication instructions](https://developer.oanda.com/rest-live-v20/authentication/) explain how to obtain a personal access token. You do not need an account ID for this candle endpoint.

Defaults monitor all three instruments on both entry timeframes, poll every 30 seconds, and request 300 candles per timeframe. The first successful poll warms up a stream and suppresses historical alerts. New events appear after future candles close. Stop with **Ctrl+C**.

For a smaller watch list or one connectivity check:

```sh
python3 -m smc_bot watch --pairs EUR_USD GBP_USD --timeframes M15
python3 -m smc_bot watch --once
```

Gold availability depends on your OANDA practice account. A provider error identifies the affected instrument; the bot continues checking the others. `--once` exits with a nonzero status if any instrument fails.

The implementation calls only the practice candle endpoint with HTTP GET. It contains no order-placement functionality. The API integration was tested with mocked responses; an authenticated connection has not been tested in this workspace.

## What makes an entry plan

These rules are one explicit interpretation of Smart Money Concepts. Both directions use mirrored rules.

1. **Trend alignment:** Each H1/H4/D chart needs at least two confirmed swing highs and two confirmed swing lows. Higher highs plus higher lows mean bullish; lower highs plus lower lows mean bearish. All three must agree. Mixed, insufficient, or stale histories block setups. A history is stale after three of its timeframe durations without another closed candle.
2. **Confirmed swings:** A swing must be strictly higher/lower than two bars on each side by default. Entry logic uses a pivot starting on the candle after its confirmation, so subsequent candles cannot rewrite an earlier signal.
3. **Liquidity sweep:** For a buy, price trades below the latest confirmed swing low and closes back above it, below the opposing swing high. For a sell, price sweeps a swing high and closes back below it, above the opposing swing low. The opposing structure level and buffered sweep stop are frozen.
4. **Structure break:** Within the next 12 candles, a later close must break the frozen opposing swing level, with a body pointing in the entry direction. A wick crossing alone does not qualify.
5. **Fair value gap (FVG):** The break must be the middle candle of a three-candle gap. For a buy, the third candle's low must exceed the first candle's high; the sell rule is mirrored. The gap must meet the configured minimum width. The plan becomes available only when the third candle closes.
6. **Price levels:** Entry is the gap midpoint; stop is beyond the sweep extreme; target is **2R** by default. For example, planned risk of 10 price points gives a target 20 points from entry.
7. **Retracement:** Monitor the next 12 completed entry candles. A valid midpoint touch emits a retracement observation. A stop touch, lost trend alignment, or opening beyond the entry cancels first. If the final eligible candle has no valid touch, the plan expires.

One plan is tracked per instrument and entry timeframe. M5 and M15 plans can coexist. Monitoring ends at the first retracement notice; the bot does not track a position or calculate trade results. Expiry counts supplied candles, so the printed expiry time is an estimate that can extend across market closures or missing data.

Higher-timeframe candles are selected by **close time as of each entry candle**, including when replaying history. All OANDA H4/daily boundaries are aligned to **midnight UTC**. This is deliberate and can differ from charts aligned to New York's trading-day close. Printed timestamps use UTC.

## Read the alerts

| Event | Meaning |
| --- | --- |
| `SETUP` | A new planned entry with FVG bounds, SL, TP, and estimated expiry |
| `RETEST` | A subsequent completed candle reached the planned entry; the notice arrives after candle close |
| `CANCELLED` | The plan failed a cancellation rule |
| `EXPIRED` | The plan did not retrace within the allowed number of candles |

These events do not establish an executable fill. The feed uses midpoint candles. An intrabar sequence cannot be reconstructed from OHLC alone, and the conservative cancellation rules can reject a touch that might have been tradable.

Use `--json` for signal JSON lines on stdout; status messages go to stderr:

```sh
python3 -m smc_bot watch --json
```

Completed-candle watermarks persist in `state.json`, suppressing repeat alerts after normal restarts. Run one process per state file; `--state another-state.json` selects a separate file. A crash between printing and saving a watermark can repeat the last alert. On catch-up, events older than two entry-timeframe durations are skipped. Changing strategy parameters does not replay previously processed candles; use `replay` to inspect history with different rules.

## Adjust the rules

```sh
python3 -m smc_bot watch --rr 3 --swing-window 3 --entry-bars 8
python3 -m smc_bot watch --help
```

| Setting | Default | Unit |
| --- | --- | --- |
| `--rr` | 2 | Planned reward/risk |
| `--swing-window` | 2 | Bars on each side of a swing |
| `--sweep-bars` | 12 | Subsequent entry candles allowed for a structure break |
| `--entry-bars` | 12 | Subsequent entry candles allowed for a retracement |
| `--fx-buffer` | 0.0001 | EUR/USD and GBP/USD quote-price distance |
| `--fx-gap` | 0.00005 | Minimum EUR/USD and GBP/USD gap width |
| `--gold-buffer` | 0.10 | Dollars beyond the gold sweep extreme |
| `--gold-gap` | 0.05 | Minimum gold gap width in dollars |

Gold settings are absolute price distances, independent of a broker's definition of a pip. Printed levels are rounded for display; JSON retains the underlying chart levels. These defaults have not been optimized.

## Offline CSV replay

Create clearly labelled synthetic files for all three instruments and replay them:

```sh
python3 -m smc_bot demo --write-data ./sample-data
python3 -m smc_bot replay --data ./sample-data
```

Replay processes historical patterns and does not report returns or accuracy. Replace synthetic data with your own completed candle exports in this layout:

```text
data/
  EUR_USD/
    M5.csv
    M15.csv
    H1.csv
    H4.csv
    D.csv
  GBP_USD/  # same five filenames
  XAU_USD/  # same five filenames
```

Each file has exactly this header, followed by timezone-aware **candle close times**, ascending and unique:

```csv
time,open,high,low,close
2026-01-12T09:05:00+00:00,1.10000,1.10040,1.09980,1.10020
```

Include enough earlier H1/H4/D history to confirm two swings of each type. Files must contain completed candles only, with matching timeframe boundaries across the bundle. An exporter using candle start timestamps must convert them to close times first.

```sh
python3 -m smc_bot replay --data ./data --pairs EUR_USD --timeframes M5
python3 -m smc_bot watch --source csv --data ./data
```

The CSV watcher rereads files each poll; update them atomically to avoid reading a partial export.

## Automated Execution & Telegram Controls (`autotrade`)

Connects the SMC signal engine directly to your OANDA practice or live broker account and pairs with a Telegram Bot for automated trade entry and remote management.

### Features
1. **Automated Order Placement:** When a confirmed SMC setup is detected on M5 or M15, the bot calculates position size based on your specified risk percentage (e.g., 1.0% of account NAV) and submits a **Limit Order** with attached **Stop Loss** and **Take Profit (2R)** directly to OANDA.
2. **Order Lifecycle Management:** If the setup is cancelled (e.g. stop touched or trend disagreement) or expires without a retracement, the bot automatically cancels the pending limit order on OANDA.
3. **Mobile Telegram Controls:** Real-time push alerts with entry, SL, TP, lot size, and broker order IDs. An interactive background listener allows you to inspect account metrics and manage risk on the go.

### Telegram Commands
| Command | Action |
| --- | --- |
| `/status` | Check auto-trade state, monitored pairs, and risk % |
| `/stats` | View performance dashboard (Win Rate, Profit Factor, Total R, Drawdown) |
| `/balance` | Check account balance, NAV, and realized P/L |
| `/trades` | List all open trades and pending limit orders |
| `/pause` | Temporarily pause automated trade placement (continues scanning and alerting) |
| `/resume` | Resume automated order placement |
| `/risk <pct>` | Dynamically update risk % per trade (e.g. `/risk 0.5` or `/risk 1.0`) |
| `/closeall` | Emergency kill-switch: cancel all pending orders and close all open trades |
| `/help` | Show available commands |

### Run Modes

**1. Live Paper Trading with Free Market Data (Zero Credentials Needed):**
Streams live real-time candles for EUR/USD, GBP/USD, and Gold from Yahoo Finance, simulates fills/stops/targets, computes institutional metrics, and alerts your Telegram:
```sh
python3 -m smc_bot autotrade --broker paper --feed yahoo --risk-pct 1.0
```

**2. Practice / Broker Execution:**
```sh
python3 -m smc_bot autotrade --broker mt5 --risk-pct 1.0
```

## Verification and API references

```sh
python3 -W error::ResourceWarning -m unittest discover -s tests -v
```

Tests cover long/short patterns, prefix-history consistency, pivot confirmation, higher-timeframe cutoff and alignment, cancellations, expiry, CSV validation, mocked OANDA responses, console warmup, and restart deduplication.

The adapter follows OANDA's [official candle API schema](https://github.com/oanda/v20-openapi/blob/master/yaml/separate/v20_instrument.yaml), [candle definitions](https://developer.oanda.com/rest-live-v20/instrument-df/), and [practice URL and connection limits](https://developer.oanda.com/rest-live-v20/development-guide/). Completed candles are used; requests are paced at least 0.55 seconds apart.
