# Stock-Monitor

A local web app for Nordic stock market data, with four automated **paper-trading** engines running alongside it.

It pulls prices, history, quotes, order books, news and corporate events from the [Millistream](https://www.millistream.com/) API. It stores everything as plain CSV and JSON files and serves a browser UI on port 8765. The engines trade simulated wallets only. No real orders are ever placed.

## Features

**Market data**

- Scrapes the instrument universe (stocks, indices, funds and more) into a browsable folder tree, sorted by instrument type and currency.
- Builds a page for each stock with an interactive price chart: candles, volume, MA20/50/200, RSI, a drag-to-measure tool and candlestick pattern detection. Each page also shows fundamentals, analyst recommendations, the order book and news.
- Includes a dashboard, a watchlist, price alerts and stock search.
- Refreshes all instruments in the background on a schedule: more often during market hours, less often off-hours and at weekends. Delisted instruments are skipped automatically.

**Paper-trading engines**

Each engine has its own wallet, log and state file, so the strategies can be compared side by side.

| Engine | Idea | Page |
|---|---|---|
| Live Signals | Mean reversion: buy a large-cap after a sharp down day, exit at a fixed target, stop or time limit | `/live-signals` |
| Live Patterns | The same exit rules, but entries come from bullish candlestick patterns on the daily bar | `/live-patterns` |
| Live Cross | Only the Signals candidates that Patterns is also firing on | `/live-cross` |
| Live Breakout | Long hold: buy fresh 52-week highs among the most-traded Swedish stocks and hold for weeks | `/live-breakout` |

`/live` shows all wallets together, including a combined equity curve. Fills can be pushed to your phone via [ntfy](https://ntfy.sh) and shown as macOS notifications.

Strategy parameters (target, stop, hold period, entry gates) change over time. For the current values, see the `/changelog` page or `strategy_versions` in `CHANGELOG.json`, not this README.

## Requirements

- Python 3.9 or newer. Only the standard library is used, so there is nothing to `pip install`.
- A Millistream API token.
- macOS, to run the engines on a schedule (see below). The web app itself runs anywhere Python does.

## Setup

1. Clone the repo, then create a `.env` file in the project root:

   ```
   MILLISTREAM_TOKEN=your-token-here

   # Optional: ntfy topic for trade notifications.
   # Treat it like a password, because anyone who knows it can read your trades.
   NTFY_TOPIC=your-private-topic
   ```

   `.env` is gitignored. Never commit it.

2. Start the server:

   ```
   python3 app.py
   ```

   This opens `http://localhost:8765` in your browser.

3. A fresh clone has no market data. `stock_names.csv` (the instrument list) is committed, but the price data is not. Open **Scrape** to build the `Instrumenttype/` folder tree, then use **Update All** to fetch prices and history.

> **Note:** the server listens on `0.0.0.0`, so anyone on your local network can reach it, and it has no login. Don't expose it to the internet.

## Running the engines

The engines run separately from the web server. `live_signals.py` is the single entry point: each run also runs Patterns, Cross and Breakout in turn. A failure in one engine never stops the others.

On macOS, schedule it with a LaunchAgent that runs every 3 minutes, plus once at 17:50 on weekdays for the end-of-day pass. The script does nothing outside trading hours, so the off-hours runs cost nothing.

```xml
<!-- ~/Library/LaunchAgents/com.example.live-signals.plist -->
<plist version="1.0">
<dict>
  <key>Label</key>             <string>com.example.live-signals</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/path/to/Stock-Monitor/live_signals.py</string>
  </array>
  <key>WorkingDirectory</key>  <string>/path/to/Stock-Monitor</string>
  <key>StartInterval</key>     <integer>180</integer>
  <key>StandardOutPath</key>   <string>/path/to/Stock-Monitor/logs/live_signals.log</string>
  <key>StandardErrorPath</key> <string>/path/to/Stock-Monitor/logs/live_signals.log</string>
</dict>
</plist>
```

Load it with:

```
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.live-signals.plist
```

Add a `StartCalendarInterval` entry for 17:50 on each weekday if you want the end-of-day run.

Things that will silently stop the engines:

- **Full Disk Access.** If the project is under `~/Desktop` or `~/Documents`, give `/usr/bin/python3` Full Disk Access in System Settings. Otherwise launchd can't read the files.
- **Sleep.** A sleeping Mac skips every tick, including when the lid is closed. Keep the machine awake during market hours, and use `pmset` to schedule a wake shortly before 17:50. Every live page shows a health banner when engines miss a session.
- **Missing log folder.** Create the `logs/` folder before loading the agent.

## Pages

| Path | What it shows |
|---|---|
| `/` | Dashboard: market indices, watchlist and a screener |
| `/stocksearch`, `/browse` | Find a stock by name, or browse the folder tree |
| `/scrape` | Build or extend the instrument universe; backfill history |
| `/live` | Overview of all paper wallets |
| `/live-signals`, `/live-patterns`, `/live-cross`, `/live-breakout` | One page per engine: open positions, closed trades and stats by version |
| `/ohlc-capture` | Status of the daily OHLC bar capture |
| `/changelog` | Version history, plus P&L grouped by strategy version |
| `/settings` | Refresh schedule, update filters, notifications, delisted instruments |
| `/help` | Feature guide and engine rules |

## Project layout

```
app.py              Web server and JSON API (port 8765)
live_signals.py     Engine entry point; also runs the other three
live_patterns.py    Candlestick-pattern engine
live_cross.py       Signals ∩ Patterns engine
live_breakout.py    52-week-high breakout engine
lib/                Shared modules: Millistream fetchers, engine helpers,
                    calendar cache, notifications, version ledger
web/                HTML pages, JavaScript and CSS
scripts/            One-off maintenance tools (deposits, backfills, smoke tests)
CHANGELOG.json      Version ledger (see below)
stock_names.csv     Instrument list: insref, name, ISIN, list/tier, sector

Not in git (created at runtime):
Instrumenttype/     Market data, one folder per instrument
state/              Wallets, settings, watchlist, alerts, caches
logs/               Engine logs
ohlc/               Daily OHLC bars captured after each close
```

Each instrument folder (`Instrumenttype/<Type>/<Currency>/<insref>_<Name>/`) holds:

- `<Name>_<insref>.csv`: all intraday prints collected so far
- `<Name>_<insref>_7d.csv`: the last 7 days of prints
- `<Name>_<insref>_History.csv`: daily closes going back to listing

Rows are `timestamp_ms, price, volume`.

## Versioning

`CHANGELOG.json` is the project's ledger, and `/changelog` displays it. It tracks two kinds of version:

- **`app_version`** goes up with every change.
- **`strategy_versions`** (for example `sig-12` or `brk-1`) go up only when an engine's trading rules change.

Every trade is stamped with both versions when it is opened. This means results can be grouped by the rules that produced them, and any trade can be traced back to the code that opened it. Every change to the code should add an entry to the ledger.

## Disclaimer

This is a personal research project. Every wallet is simulated, and backtest and paper-trading results don't predict real returns. Nothing here is investment advice.
