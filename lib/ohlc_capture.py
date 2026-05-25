"""
Daily OHLC capture for the OMXS large cap universe.

Foundation for a future candlestick-pattern auto-trading engine that will
run parallel to live_signals.py — see project_ohlc_capture memory for the
plan and the math/data constraints behind the decision to accumulate data
first and build the engine later.

This module is kept deliberately small and free of trading logic. It:
  1. Captures one OHLCV bar per trading day per stock, to `ohlc/{insref}_OHLC.csv`.
  2. Exposes `ohlc_status()` for the /ohlc-capture UI page.

The capture itself is triggered from `live_signals.run_eod_sweep()` because
that is the moment we already know the feed has settled past market close;
there is no separate LaunchAgent. If/when the patterns engine launches as
its own service, it can call into this module the same way.

File format (per stock):
    YYYY-MM-DD,open,high,low,close,volume
Prices use compact `%g` formatting (e.g. `83.96`); volume is integer.
The first column is unique per file — `capture_eod_ohlc()` is idempotent
within a day: if today's row already exists it's replaced rather than
duplicated, so multiple EOD-sweep invocations during the 17:45-18:30
window can't corrupt the file.
"""

import csv
import glob
import os

import env_loader
env_loader.load_env()
import infoScrapper    # noqa: E402

ROOT     = os.path.dirname(os.path.abspath(__file__))
OHLC_DIR = os.path.join(ROOT, "ohlc")


def capture_eod_ohlc(today, tradable, fetch_quotes_parallel):
    """Persist today's open/high/low/close/volume for every tradable ticker.

    Parameters:
      today                  — datetime.date for the bar
      tradable               — list of dicts with at least `name` and `insref`
                               (from live_signals.build_universe()'s primary)
      fetch_quotes_parallel  — callable taking the same list, returning a
                               {name: lq_dict} map with at least an `ok` flag.
                               Lets the caller share its already-fetched quote
                               cache rather than hitting the API twice — but
                               we still need a second `infoScrapper.fetch_quote`
                               per stock for the openprice/dayhighprice/
                               daylowprice fields that the simplified `lq`
                               dict doesn't carry.

    Returns the number of stocks captured (skips stocks whose quote failed
    or whose response lacked any of the OHLC fields).
    """
    if not tradable:
        return 0
    os.makedirs(OHLC_DIR, exist_ok=True)
    today_str = today.isoformat()
    captured = 0
    # The simplified lq map only tells us whether the live endpoint is alive
    # for each ticker. We still need a full fetch_quote per stock for the
    # actual OHLC fields.
    fetch_quotes_parallel(tradable)
    for t in tradable:
        insref = t["insref"]
        raw = infoScrapper.fetch_quote(insref)
        if not isinstance(raw, dict) or "error" in raw:
            continue
        try:
            o = float(raw["openprice"])
            h = float(raw["dayhighprice"])
            l = float(raw["daylowprice"])
            c = float(raw["lastprice"])
            v = float(raw.get("quantity") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        path = os.path.join(OHLC_DIR, f"{insref}_OHLC.csv")
        rows = []
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    rows = [r for r in csv.reader(f) if r and r[0] != today_str]
            except OSError:
                rows = []
        rows.append([today_str, f"{o:g}", f"{h:g}", f"{l:g}", f"{c:g}", f"{v:.0f}"])
        try:
            with open(path, "w", encoding="utf-8", newline="") as f:
                csv.writer(f).writerows(rows)
            captured += 1
        except OSError:
            pass
    return captured


def ohlc_status():
    """Snapshot of the OHLC dataset for the /ohlc-capture status page.

    Returns:
      {
        "dir":            absolute path,
        "stock_count":    N stocks with at least one bar,
        "total_bars":     sum of rows across all files,
        "earliest_date":  first date string seen anywhere (or None),
        "latest_date":    last date string seen anywhere (or None),
        "per_stock": [    sorted by insref ascending
          {"insref": int, "bars": N, "earliest": "YYYY-MM-DD",
           "latest": "YYYY-MM-DD", "latest_close": float},
          ...
        ],
        "sample_latest_bars": [
          last 5 rows of one representative stock, for sanity-check,
          [{"date": ..., "open": ..., "high": ..., "low": ..., "close": ..., "volume": ...}, ...]
        ]
      }
    """
    out = {
        "dir":             OHLC_DIR,
        "stock_count":     0,
        "total_bars":      0,
        "earliest_date":   None,
        "latest_date":     None,
        "per_stock":       [],
        "sample_latest_bars": [],
    }
    if not os.path.isdir(OHLC_DIR):
        return out
    files = sorted(glob.glob(os.path.join(OHLC_DIR, "*_OHLC.csv")))
    for fpath in files:
        fname = os.path.basename(fpath)
        try:
            insref = int(fname.split("_", 1)[0])
        except (ValueError, IndexError):
            continue
        rows = []
        try:
            with open(fpath, encoding="utf-8") as f:
                for r in csv.reader(f):
                    if len(r) >= 6 and r[0]:
                        rows.append(r)
        except OSError:
            continue
        if not rows:
            continue
        out["stock_count"] += 1
        out["total_bars"]  += len(rows)
        first_d, last_d = rows[0][0], rows[-1][0]
        if out["earliest_date"] is None or first_d < out["earliest_date"]:
            out["earliest_date"] = first_d
        if out["latest_date"] is None or last_d > out["latest_date"]:
            out["latest_date"] = last_d
        try:
            latest_close = float(rows[-1][4])
        except (ValueError, IndexError):
            latest_close = None
        out["per_stock"].append({
            "insref":       insref,
            "bars":         len(rows),
            "earliest":     first_d,
            "latest":       last_d,
            "latest_close": latest_close,
        })

    # Sample: last 5 bars of the first stock so the user can sanity-check shape
    if files:
        try:
            with open(files[0], encoding="utf-8") as f:
                tail = list(csv.reader(f))[-5:]
            out["sample_latest_bars"] = [
                {
                    "date":   r[0],
                    "open":   float(r[1]),
                    "high":   float(r[2]),
                    "low":    float(r[3]),
                    "close":  float(r[4]),
                    "volume": float(r[5]),
                }
                for r in tail if len(r) >= 6
            ]
            out["sample_insref"] = os.path.basename(files[0]).split("_", 1)[0]
        except (OSError, ValueError):
            pass
    return out
