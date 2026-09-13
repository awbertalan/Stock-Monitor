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
from collections import defaultdict
from datetime import datetime, timezone

import env_loader
env_loader.load_env()
import infoScrapper    # noqa: E402
import engine_common as ec    # noqa: E402

ROOT     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # project root (this module lives in lib/)
OHLC_DIR = os.path.join(ROOT, "ohlc")
BASE     = os.path.join(ROOT, "Instrumenttype", "Equity", "SEK")
# Sidecar next to the bars, NOT inside ohlc/*_OHLC.csv, so the *_OHLC.csv glob
# in ohlc_status()/the patterns engine never picks it up as a stock.
SKIPS_PATH = os.path.join(OHLC_DIR, "_capture_skips.json")

# insrefs whose existing history could not be read during the last
# capture_eod_ohlc() call, and were therefore left untouched. Kept as a module
# attribute rather than folded into the return value because the caller
# (live_signals.maybe_capture_eod_ohlc) stores that return as a plain bar count
# in `ohlc_bars_total`; a silent skip is still a skip, so it has to be readable
# somewhere. NOTE: no caller reads this attribute today — it is an in-process
# debugging hook only. The channel that is actually wired is SKIPS_PATH below.
#
# The module attribute alone is not enough: capture runs inside the live_signals
# process, while the /ohlc-capture page is served by app.py — a different
# process with its own import of this module, where the attribute is forever
# empty. So the same record is also persisted to SKIPS_PATH and read back by
# ohlc_status(), which is the only channel the UI can actually see.
LAST_CAPTURE_SKIPPED = []


def _record_capture(today_str, captured, skipped):
    """Publish this run's skip record to the module attribute and SKIPS_PATH.

    Called on every path that counts as "capture ran today", including the
    empty-universe one: the caller stamps `last_ohlc_capture_date` regardless
    of what we return, so a sidecar left holding yesterday's date/counts would
    be read as today's result. A failure to write the sidecar must not cost us
    the day's bars, hence the guarded write.
    """
    global LAST_CAPTURE_SKIPPED
    LAST_CAPTURE_SKIPPED = skipped
    try:
        ec.write_json_atomic(SKIPS_PATH, {
            "date":     today_str,
            "captured": captured,
            "skipped":  skipped,
        })
    except OSError as e:
        print(f" OHLC capture: could not record skip status to "
              f"{SKIPS_PATH} ({e})")


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

    Returns the number of stocks captured (skips stocks whose quote failed,
    whose response lacked any of the OHLC fields, or whose existing history
    could not be re-read — see LAST_CAPTURE_SKIPPED).
    """
    os.makedirs(OHLC_DIR, exist_ok=True)
    today_str = today.isoformat()
    if not tradable:
        # An empty universe (feed outage, filter bug) still has to clear the
        # sidecar. Without this the record stays on the last day that did run,
        # and a reader sees that day's skips reported as today's.
        _record_capture(today_str, 0, [])
        return 0
    captured = 0
    skipped = []
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
            except OSError as e:
                # A failed read used to fall through with rows=[], and the write
                # below then replaced the whole accumulated file with today's
                # single bar. The file *exists*, so this is a transient error
                # (EMFILE while the refresh workers hold every fd, EIO, a stale
                # handle) — never an empty history. Skip the stock instead; the
                # bar is one day, the file is every day since 2026-05-23 and the
                # API cannot re-serve it.
                skipped.append({"insref": insref,
                                "name":   t.get("name"),
                                "error":  str(e)})
                print(f" OHLC capture: SKIPPED {t.get('name') or insref} "
                      f"({insref}) — existing bars unreadable ({e}); "
                      f"today's bar not written rather than risk the history")
                continue
        rows.append([today_str, f"{o:g}", f"{h:g}", f"{l:g}", f"{c:g}", f"{v:.0f}"])
        try:
            # Atomic swap: the destination is only ever the old file or the
            # complete new one, so a kill or power loss mid-write (launchd
            # timeout, laptop sleep) can no longer truncate the history.
            ec.write_rows_atomic(path, rows)
            captured += 1
        except OSError:
            pass
    # Written on every run, including the clean one: an empty list is what
    # clears a warning raised for a previous day.
    _record_capture(today_str, captured, skipped)
    if skipped:
        names = [sk.get("name") or sk["insref"] for sk in skipped]
        print(f" OHLC capture: {len(skipped)} stock(s) left untouched because "
              f"their existing bars could not be read: {names}")
    return captured


def backfill_from_intraday(skip_today=True):
    """One-shot historical backfill: aggregate each `*_7d.csv` into daily
    OHLC bars and write them to `ohlc/{insref}_OHLC.csv`. Only fills bars
    that are NOT already present in the per-stock CSV — safe to re-run.

    Returns {"stocks_touched": int, "bars_added": int, "bars_skipped": int,
    "stocks_unreadable": int} — the last being stocks whose existing OHLC file
    could not be read and were therefore left alone.

    Caveats:
      · *_7d.csv carries minute closes only, not true OHLC. The derived
        O/H/L/C use first/max/min/last of minute closes — identical to the
        proxy live_patterns.py already uses. Real EOD captures (which start
        accumulating Monday 2026-06-15 via maybe_capture_eod_ohlc) will use
        proper Millistream openprice/dayhighprice/daylowprice fields.
      · Volume cannot be reconstructed from minute closes; backfilled rows
        carry volume=0. Real captures fill volume correctly.
      · skip_today=True (default) drops today's in-progress bar so we don't
        write a partial day. Set False for a synthetic test.
    """
    os.makedirs(OHLC_DIR, exist_ok=True)
    today_str = datetime.now(timezone.utc).date().isoformat()
    folders = sorted(glob.glob(os.path.join(BASE, "*")))
    stocks_touched    = 0
    bars_added        = 0
    bars_skipped      = 0
    stocks_unreadable = 0
    for folder in folders:
        basename = os.path.basename(folder)
        try:
            insref = int(basename.split("_", 1)[0])
        except (ValueError, IndexError):
            continue
        cand = glob.glob(os.path.join(folder, "*_7d.csv"))
        if not cand:
            continue
        minute_rows = []
        try:
            with open(cand[0], encoding="utf-8") as f:
                for r in csv.reader(f):
                    try:
                        minute_rows.append((int(float(r[0])), float(r[1])))
                    except (ValueError, IndexError):
                        pass
        except OSError:
            continue
        if not minute_rows:
            continue
        minute_rows.sort()
        # Aggregate to daily buckets (UTC date)
        by_day = defaultdict(list)
        for ts, p in minute_rows:
            d = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).date().isoformat()
            by_day[d].append(p)
        out_path = os.path.join(OHLC_DIR, f"{insref}_OHLC.csv")
        existing_dates = set()
        existing_rows  = []
        if os.path.exists(out_path):
            try:
                with open(out_path, encoding="utf-8") as f:
                    for r in csv.reader(f):
                        if r:
                            existing_rows.append(r)
                            existing_dates.add(r[0])
            except OSError as e:
                # Same trap as in capture_eod_ohlc(): swallowing the error left
                # existing_rows empty, and the `merged` write below then wrote
                # only the backfilled bars over a file that already held real
                # captures. The file exists, so an unreadable one means a
                # transient error — leave the stock alone.
                stocks_unreadable += 1
                print(f"  backfill: SKIPPED insref {insref} — existing bars "
                      f"unreadable ({e}); left untouched")
                continue
        new_rows = []
        for d in sorted(by_day):
            if skip_today and d == today_str:
                continue
            if d in existing_dates:
                bars_skipped += 1
                continue
            prices = by_day[d]
            new_rows.append([d, f"{prices[0]:g}", f"{max(prices):g}",
                             f"{min(prices):g}", f"{prices[-1]:g}", "0"])
        if not new_rows:
            continue
        merged = sorted(existing_rows + new_rows, key=lambda r: r[0])
        try:
            # tmp + os.replace — see the note in capture_eod_ohlc().
            ec.write_rows_atomic(out_path, merged)
            stocks_touched += 1
            bars_added += len(new_rows)
        except OSError:
            pass
    return {
        "stocks_touched":    stocks_touched,
        "bars_added":        bars_added,
        "bars_skipped":      bars_skipped,
        "stocks_unreadable": stocks_unreadable,
    }


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
        ],
        "last_capture":   {"date": ..., "captured": N,
                           "skipped": [{"insref": ..., "name": ..., "error": ...}]}
                          — the SKIPS_PATH sidecar, or None if capture has not
                          run since this was added. A non-empty `skipped` means
                          those stocks kept their history but have NO bar for
                          that date. NOT RENDERED YET: web/ohlc_capture.html
                          draws only stock_count/total_bars and app.py's health
                          block keys off last_ohlc_capture_date, so a run with
                          skips still *looks* clean on the page. The data is
                          served here so the page can be wired; until it is,
                          the skips exist only in this payload and in logs/.
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
        "last_capture":    ec.read_json(SKIPS_PATH, None),
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
