"""
One-shot OHLC backfill from the rolling *_7d.csv intraday files.

Run once to seed the `ohlc/` historical store with the last ~5-7 days of
daily bars derived from existing minute-close data. Subsequent days are
filled in automatically by `maybe_capture_eod_ohlc()` in live_signals.py
via the LaunchAgent's post-close ticks.

Why this exists: real EOD capture was broken until 2026-06-13 (see
project_ohlc_capture memory). Real captures only accumulate going forward
from Mon 2026-06-15 — but the next-bar-confirmation backtest for the
Hammer pattern (audit follow-up #4) needs a usable history NOW. The
intraday CSVs already exist and aggregate cleanly to daily bars; we just
weren't persisting that aggregation.

Usage:
    python3 scripts/backfill_ohlc.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))

import ohlc_capture   # noqa: E402


def main():
    print("OHLC backfill from intraday *_7d.csv files")
    print("=" * 60)
    print(f"  source dir: {ohlc_capture.BASE}")
    print(f"  output dir: {ohlc_capture.OHLC_DIR}")
    print()
    result = ohlc_capture.backfill_from_intraday(skip_today=True)
    print(f"  stocks touched: {result['stocks_touched']}")
    print(f"  bars added    : {result['bars_added']}")
    print(f"  bars skipped  : {result['bars_skipped']} (already in ohlc/)")
    print()
    status = ohlc_capture.ohlc_status()
    print(f"After backfill: {status['stock_count']} stocks, "
          f"{status['total_bars']} total bars, "
          f"earliest={status['earliest_date']}, latest={status['latest_date']}")


if __name__ == "__main__":
    main()
