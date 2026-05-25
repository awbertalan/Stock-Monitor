"""
Live paper-trading signal generator.

Strategy: +2% in <= 7 trading days — VALIDATED mean-reversion (backtested
+0.28% per trade across 12,262 OMXS30 entries over full daily history; the
prior 0.5%/-2.0%/5d pullback strategy was -EV at -0.29% per trade and was
retired 2026-05-23 — see project_live_strategy_negative_ev memory).

  Entry (computed on latest close, refreshed each run with the live quote):
    1) Day-over-day return <= -1%     (close[D] / close[D-1] - 1 <= -0.01)
    2) close[D] strictly below the 5-day high (close[D] < max(close[-5:]))
    3) Not a Friday entry             (avoid carrying over the weekend)
    4) 20-day return > -15%           (crash guard — skip total-blowout regimes)
  Sizing:    single tier against a 500k SEK wallet, floor 35k:
               target 70k, cap 85k
  Exits:     TP +2.0%, SL -2.0%, time-out at 7 trading days  (limit-fill capped)
             — SL widened from -1.0% to -2.0% on 2026-05-25 after observing
             that opening-volatility was knocking out many valid mean-reversion
             trades on the -1% stop. Note: this raises the break-even WR from
             33% (at 2:1 reward:risk) to 50% (at 1:1) — re-check EV in ~30 trades.
  Cooldown:  SL exits block re-entry on the same ticker for 7 trading days;
             TP/TIMEOUT exits block same-day re-entry only.
  Approval:  watchlist tickers approved via the UI are one-shot — the approval
             is consumed automatically the first time a position is opened

Universe:
  Primary   = every OMX large cap with sufficient history (auto-loaded each
              run from stock_names.csv, so re-classifications drop out
              automatically).
  Watchlist = every OMX mid/small cap with sufficient history. We do NOT take
              positions here (order size > available volume), but we surface
              any signal so the user can manually evaluate.

State is persisted in live_portfolio.json so this script can be invoked
repeatedly (e.g. every 3 min) without double-entering positions.
"""

import csv
import glob
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

import env_loader      # noqa: E402
env_loader.load_env()
import infoScrapper    # noqa: E402
import tradesScrapper  # noqa: E402
import ohlc_capture    # noqa: E402
import calendar_cache  # noqa: E402

ROOT          = os.path.dirname(os.path.abspath(__file__))
STATE_DIR     = os.path.join(ROOT, "state")
LOGS_DIR      = os.path.join(ROOT, "logs")
BASE          = os.path.join(ROOT, "Instrumenttype", "Equity", "SEK")
STATE_PATH    = os.path.join(STATE_DIR, "live_portfolio.json")
APPROVED_PATH    = os.path.join(STATE_DIR, "live_approved.json")
FORCE_ENTRY_PATH = os.path.join(STATE_DIR, "live_force_entry.json")
FORCE_SELL_PATH  = os.path.join(STATE_DIR, "live_force_sell.json")
LOG_PATH      = os.path.join(LOGS_DIR,  "live_signals.log")
SPX_HIST_GLOB = os.path.join(ROOT, "Instrumenttype", "Index", "USD",
                              "72823_*", "*_History.csv")

# Log retention: at the top of every run, trim live_signals.log so it only
# keeps roughly the last 24h of output. Prevents the file from growing
# unboundedly under the 3-min cadence (~170 runs/day during market hours).
LOG_RETAIN_HOURS = 24
LOG_ROTATE_MIN_BYTES = 200_000   # don't bother rotating when file is small
NAMES_CSV   = os.path.join(ROOT, "stock_names.csv")

WALLET_START  = 500_000.0
POS_MIN       =  35_000.0  # global floor — every tier rejects below this after rounding

# Position size scales with conviction. (target SEK, cap SEK) per tier.
# Validated strategy uses a single tier; the "high"/"low" rows are kept for
# forward-compatibility with already-open positions saved before 2026-05-23.
CONVICTION_SIZING = {
    "high":   (100_000.0, 120_000.0),
    "normal": ( 70_000.0,  85_000.0),  # default for VALIDATED entries
    "low":    ( 50_000.0,  60_000.0),
}

TP_PCT        = 0.020   # +2.0%
SL_PCT        = -0.020  # -2.0% (widened from -1.0% on 2026-05-25 to survive opening volatility)
MAX_HOLD_DAYS = 7
COOLDOWN_DAYS = 7       # block re-entry on a ticker for N trading days after an SL exit only
DOD_THRESHOLD = -0.010  # require day-over-day close <= -1% to enter
CRASH_GUARD   = -0.15   # require 20-day ret > -15% (skip total-blowout regimes)

LIVE_FETCH_WORKERS = 10
MIN_HISTORY_DAYS   = 30

# Single liquid ticker used as a data-feed lag probe (Millistream quote command
# does not expose a tradetime; we infer lag from the most recent trade on SEB A).
LAG_PROBE_INSREF   = 342
LAG_PROBE_NAME     = "SEB A"

# Post-close EOD sweep window. The Millistream feed lags real-time by ~15-16 min,
# so the 17:30 closing-auction prints don't show up until ~17:45. We allow any
# scheduled run between 17:45 and 18:30 to do one final exit check against the
# settled closing prices — gated by both clock time and the lag-probe trade time.
EOD_WINDOW_START = (17, 45)
EOD_WINDOW_END   = (18, 30)
MARKET_CLOSE     = (17, 30)


# ----------------------------------------------------------------------------- helpers

def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            return json.load(f)
    return {
        "cash":              WALLET_START,
        "positions":         {},
        "closed_trades":     [],
        "last_run":          None,
        "last_universe":     [],
        "last_watchlist":    [],
    }


def save_state(state):
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2, default=str)


def load_approved():
    """Load the list of watchlist tickers the user has approved for trading."""
    if not os.path.exists(APPROVED_PATH):
        return set()
    try:
        with open(APPROVED_PATH) as f:
            data = json.load(f)
        return set(data) if isinstance(data, list) else set()
    except (json.JSONDecodeError, OSError):
        return set()


def consume_approvals(consumed):
    """Remove `consumed` tickers from the approved file. Re-reads first so any
    new approvals added via the web UI during the run aren't clobbered."""
    if not consumed:
        return
    fresh = load_approved()
    remaining = fresh - set(consumed)
    with open(APPROVED_PATH, "w") as f:
        json.dump(sorted(remaining), f, ensure_ascii=False, indent=2)


def load_force_entries():
    """Tickers the user has approved for forced entry on the next signal check
    — bypasses the validated filter entirely. Use case: a near-miss the user
    wants to take anyway (e.g. DoD -0.9% instead of the required -1.0%).
    One-shot — consumed on entry, like watchlist approvals."""
    if not os.path.exists(FORCE_ENTRY_PATH):
        return set()
    try:
        with open(FORCE_ENTRY_PATH) as f:
            data = json.load(f)
        return set(data) if isinstance(data, list) else set()
    except (json.JSONDecodeError, OSError):
        return set()


def consume_force_entries(consumed):
    if not consumed:
        return
    fresh = load_force_entries()
    remaining = fresh - set(consumed)
    with open(FORCE_ENTRY_PATH, "w") as f:
        json.dump(sorted(remaining), f, ensure_ascii=False, indent=2)


def load_force_sells(path=None):
    """Tickers the user has queued for an immediate market sell on the next
    engine run. Bypasses TP/SL/TIMEOUT — exits at the current live price with
    reason='MANUAL'. One-shot: consumed once the exit is recorded.

    `path` defaults to the Live Signals queue but the same helper is reused
    by live_patterns.py with its own per-engine path."""
    p = path or FORCE_SELL_PATH
    if not os.path.exists(p):
        return set()
    try:
        with open(p) as f:
            data = json.load(f)
        return set(data) if isinstance(data, list) else set()
    except (json.JSONDecodeError, OSError):
        return set()


def consume_force_sells(consumed, path=None):
    if not consumed:
        return
    p = path or FORCE_SELL_PATH
    fresh = load_force_sells(p)
    remaining = fresh - set(consumed)
    with open(p, "w") as f:
        json.dump(sorted(remaining), f, ensure_ascii=False, indent=2)


def load_history_closes(path):
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.reader(f):
            if not r:
                continue
            try:
                ts_ms = int(float(r[0]))
                price = float(r[1])
                rows.append((ts_ms, price))
            except (ValueError, IndexError):
                pass
    rows.sort(key=lambda x: x[0])
    return rows


def rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    window = closes[-(period + 1):]
    gains = losses = 0.0
    for i in range(1, len(window)):
        ch = window[i] - window[i - 1]
        if ch > 0:
            gains += ch
        else:
            losses -= ch
    avg_g = gains / period
    avg_l = losses / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100 - 100 / (1 + rs)


def add_trading_days(start_date, n):
    d = start_date
    added = 0
    while added < n:
        d += timedelta(days=1)
        if d.weekday() < 5:
            added += 1
    return d


def trading_days_between(d1, d2):
    """Count trading days strictly after d1 up to and including d2."""
    if isinstance(d1, str):
        d1 = datetime.fromisoformat(d1).date()
    if d2 <= d1:
        return 0
    days = 0
    d = d1
    while d < d2:
        d += timedelta(days=1)
        if d.weekday() < 5:
            days += 1
    return days


def signal_check(closes, today_date=None):
    """VALIDATED mean-reversion entry filter.

    All four conditions must be true to pass:
      1) Day-over-day return <= -1%   (today closed at least 1% below yesterday)
      2) Today's close is NOT at the 5-day high
      3) Today is not a Friday        (no carry over the weekend)
      4) 20-day return > -15%         (crash guard)

    closes      — list of closes ending with today's value (live quote tacked on)
    today_date  — optional datetime.date for the Friday check. If None, today is
                  derived from datetime.now() in the caller's tz; this matches
                  how the live engine invokes this function during market hours.
    """
    if len(closes) < 21:
        return False, {"err": "insufficient history"}
    today_p, yest_p = closes[-1], closes[-2]
    dod    = today_p / yest_p - 1.0 if yest_p else 0.0
    win5   = closes[-5:]
    hi5    = max(win5)
    at_hi5 = today_p >= hi5
    ret20  = today_p / closes[-21] - 1.0
    if today_date is None:
        today_date = datetime.now().date()
    is_friday = today_date.weekday() == 4
    passes = (
        dod <= DOD_THRESHOLD
        and not at_hi5
        and not is_friday
        and ret20 > CRASH_GUARD
    )
    return passes, {
        "dod":         dod,
        "at_5d_high":  at_hi5,
        "ret_20d":     ret20,
        "is_friday":   is_friday,
        "today":       today_p,
        "yest":        yest_p,
        "entry_tag":   "validated",
        "conviction":  "normal",
    }


def probe_data_lag():
    """
    Fetch the most recent trade for the probe ticker and compute the gap
    between its timestamp and 'now'. Returns:
      {trade_dt: datetime|None, lag_seconds: int|None, ticker: str, error: str?}
    Times are interpreted in local Stockholm time (the Millistream feed
    reports market local time without timezone).
    """
    try:
        data = tradesScrapper.fetch_trades(LAG_PROBE_INSREF, limit=1)
    except Exception as e:
        return {"trade_dt": None, "lag_seconds": None, "ticker": LAG_PROBE_NAME, "error": str(e)}

    trades = data.get("trade") if isinstance(data, dict) else None
    if not trades:
        return {"trade_dt": None, "lag_seconds": None, "ticker": LAG_PROBE_NAME, "error": "no trades"}

    t = trades[0]
    try:
        trade_dt = datetime.strptime(f"{t['date']} {t['time']}", "%Y-%m-%d %H:%M:%S")
    except (KeyError, ValueError) as e:
        return {"trade_dt": None, "lag_seconds": None, "ticker": LAG_PROBE_NAME, "error": str(e)}

    now_local = datetime.now()
    lag = (now_local - trade_dt).total_seconds()
    return {
        "trade_dt":     trade_dt.isoformat(),
        "lag_seconds":  int(lag),
        "ticker":       LAG_PROBE_NAME,
    }


def rotate_log():
    """Trim live_signals.log so it only keeps the last LOG_RETAIN_HOURS of
    output. Called at the very top of main()/EOD-sweep before any print, so
    there's no race with our own stdout. launchd's open file descriptor is
    unaffected — `open(LOG_PATH, "wb")` truncates the same inode in place,
    and the next print() in this process is appended at the new end.

    A run is kept iff its `Live signal run @ <ts>` header is within the
    retention window. Skips work entirely when the file is below
    LOG_ROTATE_MIN_BYTES so the 3-min cron isn't doing useless I/O."""
    try:
        size = os.path.getsize(LOG_PATH)
    except OSError:
        return
    if size < LOG_ROTATE_MIN_BYTES:
        return
    cutoff = (datetime.now() - timedelta(hours=LOG_RETAIN_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return
    # Find the index of the first run header at or after the cutoff
    keep_from = None
    for i, ln in enumerate(lines):
        if " Live signal run @ " in ln:
            ts_str = ln.split("@", 1)[1].strip().replace(" local", "").strip()
            if ts_str >= cutoff:
                keep_from = i
                break
    if keep_from is None:
        # No run within the retention window — file is pure history, drop it
        # entirely (next run will write a fresh header).
        try:
            with open(LOG_PATH, "wb"): pass
        except OSError:
            pass
        return
    if keep_from == 0:
        return  # already starts within the window, nothing to trim
    # The `===` separator immediately before the run header is also worth
    # keeping so the first surviving run still has a header banner.
    if keep_from > 0 and lines[keep_from - 1].startswith("==="):
        keep_from -= 1
    new_content = "".join(lines[keep_from:])
    try:
        with open(LOG_PATH, "wb") as f:
            f.write(new_content.encode("utf-8"))
    except OSError:
        pass


def read_spx_context():
    """Most recent S&P 500 daily return, for tape-regime context on the UI.

    Reads the locally-cached SP500 daily History.csv (no extra API call).
    The smoketest on 2026-05-23 showed yesterday's SPX return does NOT
    translate into an actionable strategy filter — we surface it purely as
    awareness of the global regime at the moment of an entry decision.

    Returns a dict {date, return_pct, stale_days} or None if the file is
    missing / too short. `stale_days` counts calendar days since the last
    available close, so the UI can grey out the pill when data goes stale.
    """
    candidates = glob.glob(SPX_HIST_GLOB)
    if not candidates:
        return None
    try:
        rows = load_history_closes(candidates[0])
    except OSError:
        return None
    if len(rows) < 2:
        return None
    last_ts, last_p = rows[-1]
    prev_ts, prev_p = rows[-2]
    if prev_p <= 0:
        return None
    last_d = datetime.fromtimestamp(last_ts / 1000, tz=timezone.utc).date()
    return {
        "date":        last_d.isoformat(),
        "return_pct":  (last_p / prev_p - 1.0) * 100,
        "stale_days":  (datetime.now().date() - last_d).days,
    }


def format_signal_tag(info):
    """Compact tag for log lines. VALIDATED entries are tagged [VAL]; older
    pre-2026-05-23 trades (entry_tag == 'standard' / 'deep-pullback') keep
    their legacy [STD] / [STD/HIGH] / [DEEP-PB] tags so historical log lines
    and the closed-trades table remain readable. -WL suffix indicates the
    entry came from a user-approved watchlist (mid/small cap) ticker."""
    if not info:
        return "[VAL]"
    tag = info.get("entry_tag")
    if tag == "forced":
        base = "[FORCED]"
    elif tag == "validated":
        base = "[VAL]"
    elif tag == "deep-pullback":
        base = "[DEEP-PB]"
    elif info.get("conviction") == "high":
        base = "[STD/HIGH]"
    else:
        base = "[STD]"
    if info.get("from_watchlist"):
        return base[:-1] + "-WL]"
    return base


def company_stem(name):
    """Strip share-class / form suffix so A and B of the same company collapse."""
    for s in (" SDB", " Pref", " A", " B", " C", " D", " R"):
        if name.endswith(s):
            return name[:-len(s)].rstrip()
    return name


def is_market_open(now_local=None):
    if now_local is None:
        now_local = datetime.now()
    if now_local.weekday() >= 5:
        return False
    mins = now_local.hour * 60 + now_local.minute
    return 9 * 60 <= mins <= 17 * 60 + 30


def is_in_eod_window(now_local=None):
    if now_local is None:
        now_local = datetime.now()
    if now_local.weekday() >= 5:
        return False
    mins  = now_local.hour * 60 + now_local.minute
    start = EOD_WINDOW_START[0] * 60 + EOD_WINDOW_START[1]
    end   = EOD_WINDOW_END[0]   * 60 + EOD_WINDOW_END[1]
    return start <= mins <= end


# ----------------------------------------------------------------------------- universe

def _find_history_csv(insref):
    """Return (folder_name, csv_path) for an insref, or (None, None) if missing."""
    matches = glob.glob(os.path.join(BASE, f"{insref}_*"))
    if not matches:
        return None, None
    folder = matches[0]
    hist = glob.glob(os.path.join(folder, "*_History.csv"))
    if not hist:
        return os.path.basename(folder), None
    return os.path.basename(folder), hist[0]


def build_universe():
    """
    Returns (primary, watchlist) where each is a list of dicts:
      {name, insref, folder, csv, tier}
    primary = large cap, watchlist = mid/small cap.
    Filters out tickers with no history CSV or <30 rows.
    """
    primary, watchlist = [], []
    if not os.path.exists(NAMES_CSV):
        return primary, watchlist

    with open(NAMES_CSV, newline="", encoding="utf-8") as f:
        for row in csv.reader(f):
            if len(row) < 4:
                continue
            insref_raw, name, tier = row[0], row[1], row[-1].strip()
            try:
                insref = int(insref_raw)
            except ValueError:
                continue
            if tier not in ("large cap", "mid cap", "small cap"):
                continue
            folder, csv_path = _find_history_csv(insref)
            if not csv_path:
                continue
            # Quick row count check
            with open(csv_path, encoding="utf-8") as fh:
                n_rows = sum(1 for _ in fh)
            if n_rows < MIN_HISTORY_DAYS:
                continue
            entry = {
                "name":   name,
                "insref": insref,
                "folder": folder,
                "csv":    csv_path,
                "tier":   tier,
            }
            (primary if tier == "large cap" else watchlist).append(entry)
    return primary, watchlist


def get_live(insref):
    q = infoScrapper.fetch_quote(insref)
    if isinstance(q, dict) and "error" not in q:
        return {
            "last":    q.get("lastprice"),
            "bid":     q.get("bidprice"),
            "ask":     q.get("askprice"),
            "diffprc": q.get("diff1dprc"),
            "ok":      True,
        }
    return {"ok": False, "err": (q.get("error") if isinstance(q, dict) else "unknown")}


def fetch_live_parallel(tickers):
    """tickers: iterable of dicts with 'name' and 'insref'."""
    out = {}
    with ThreadPoolExecutor(max_workers=LIVE_FETCH_WORKERS) as ex:
        futs = {ex.submit(get_live, t["insref"]): t["name"] for t in tickers}
        for fut in as_completed(futs):
            out[futs[fut]] = fut.result()
    return out


def apply_exit_checks(state, today, now_local_ts, live, force_sells=None):
    """Close any open positions whose current live price triggered TP/SL/timeout.
    Also honours a user-requested manual exit set: tickers in `force_sells`
    are sold at the current live price (no limit-fill cap — the user pressed
    "Sell now"), recorded with reason='MANUAL'. Force-sell takes priority
    over TP/SL/TIMEOUT so a position queued for exit gets sold regardless of
    where the price is. Mutates state and returns the list of trades closed."""
    if force_sells is None:
        force_sells = set()
    closed_now = []
    for tk in list(state["positions"].keys()):
        pos = state["positions"][tk]
        cur_price = live.get(tk, {}).get("last")
        if cur_price is None:
            continue
        target_date = (datetime.fromisoformat(pos["target_exit_date"]).date()
                       if isinstance(pos["target_exit_date"], str)
                       else pos["target_exit_date"])

        exit_reason = None
        fill_price = cur_price
        if tk in force_sells:
            exit_reason = "MANUAL"   # user-requested sell-now via UI
            fill_price = cur_price   # market-style fill at current price
        elif cur_price >= pos["tp"]:
            exit_reason = "TP"
            fill_price = pos["tp"]   # limit-sell @ TP — fills at TP, not above
        elif cur_price <= pos["sl"]:
            exit_reason = "SL"
            fill_price = pos["sl"]   # stop-loss @ SL — fills at SL, not below
        elif today >= target_date:
            exit_reason = "TIMEOUT"

        if exit_reason:
            proceeds = pos["shares"] * fill_price
            pnl = proceeds - pos["shares"] * pos["entry_price"]
            state["cash"] += proceeds
            closed = {
                "ticker":      tk,
                "entry_date":  pos["entry_date"],
                "entry_ts":    pos.get("entry_ts"),
                "exit_date":   today.isoformat(),
                "exit_ts":     now_local_ts,
                "entry_price": pos["entry_price"],
                "exit_price":  fill_price,
                "shares":      pos["shares"],
                "pnl_sek":     pnl,
                "ret_pct":     fill_price / pos["entry_price"] - 1.0,
                "reason":      exit_reason,
                "entry_tag":   (pos.get("signal_info") or {}).get("entry_tag", "standard"),
                "conviction":  (pos.get("signal_info") or {}).get("conviction", "normal"),
                "signal_info": pos.get("signal_info"),
            }
            state["closed_trades"].append(closed)
            closed_now.append(closed)
            del state["positions"][tk]
    return closed_now


def run_eod_sweep(state, today, now_local_ts):
    """One final post-close sweep that polls open-position quotes and applies
    TP/SL/timeout exits against the closing prices. The Millistream feed lags
    ~15-16 min, so even after 17:30 the closing-auction prints take a few
    minutes to appear. We probe the feed via SEB A and only run the sweep
    if the feed has already advanced past the 17:30 close; otherwise we bail
    and let the next scheduled run try again (the EOD window is wide enough
    to retry several times). No universe scan, no new entries — exit-only."""
    print(" EOD sweep: checking open positions against closing prices")

    lag = probe_data_lag()
    state["data_lag"] = lag
    state["us_context"] = read_spx_context()
    trade_dt_iso = lag.get("trade_dt")
    trade_dt = None
    if trade_dt_iso:
        try:
            trade_dt = datetime.fromisoformat(trade_dt_iso)
        except ValueError:
            trade_dt = None
    if trade_dt is None:
        print(f"   feed probe failed ({lag.get('error', 'unknown')}); retry next run")
        save_state(state)
        return

    close_time = trade_dt.replace(hour=MARKET_CLOSE[0], minute=MARKET_CLOSE[1],
                                  second=0, microsecond=0)
    if trade_dt < close_time:
        gap = int((close_time - trade_dt).total_seconds() // 60)
        print(f"   feed only at {trade_dt.strftime('%H:%M:%S')} "
              f"({gap}m before close); retry next run")
        save_state(state)
        return
    print(f"   feed settled (last {lag['ticker']} trade @ "
          f"{trade_dt.strftime('%H:%M:%S')})")

    if not state["positions"]:
        print("   no open positions to check")
        state["last_eod_sweep_date"] = today.isoformat()
        save_state(state)
        return

    live = {}
    for tk, pos in state["positions"].items():
        insref = pos.get("insref")
        if not insref:
            continue
        q = get_live(insref)
        if q.get("ok"):
            q["source"] = "live-eod"
            live[tk] = q
        else:
            print(f"   could not fetch quote for {tk}: {q.get('err', 'unknown')}")

    force_sells = load_force_sells()
    closed_now  = apply_exit_checks(state, today, now_local_ts, live, force_sells)
    consume_force_sells({t["ticker"] for t in closed_now if t["reason"] == "MANUAL"})

    last_quotes = state.get("last_quotes", {})
    for tk, lq in live.items():
        last_quotes[tk] = {
            "last":     lq.get("last"),
            "bid":      lq.get("bid"),
            "ask":      lq.get("ask"),
            "diff_pct": lq.get("diffprc"),
            "source":   "live-eod",
        }
    state["last_quotes"] = last_quotes

    if closed_now:
        print(f"\n--- CLOSED ON EOD SWEEP ({len(closed_now)}) ---")
        for t in closed_now:
            tag_str = format_signal_tag(t.get("signal_info"))
            print(f"  {tag_str:<10} {t['ticker']:<22} {t['reason']:<7} @ "
                  f"{t['exit_price']:.2f}   "
                  f"P&L {t['pnl_sek']:+,.0f} SEK ({t['ret_pct']*100:+.2f}%)   "
                  f"opened {t['entry_date']}")
    else:
        print("   no exits triggered at close")

    if state["positions"]:
        print(f"\n--- STILL OPEN AFTER CLOSE ({len(state['positions'])}) ---")
        for tk, pos in state["positions"].items():
            cur_price = live.get(tk, {}).get("last") or pos["entry_price"]
            pnl = pos["shares"] * (cur_price - pos["entry_price"])
            ret = cur_price / pos["entry_price"] - 1
            tag_str = format_signal_tag(pos.get("signal_info"))
            print(f"  {tag_str:<10} {tk:<22} {pos['shares']:>5} sh   "
                  f"entered {pos['entry_date']} @ {pos['entry_price']:.2f}   "
                  f"close {cur_price:.2f}   P&L {pnl:+,.0f} SEK ({ret*100:+.2f}%)")

    # Capture today's OHLC for every tradable ticker so a future
    # candlestick-pattern engine has bars to operate on. Foundation laid
    # 2026-05-23; engine to be built once ~30-60 days of bars accumulate.
    primary, _ = build_universe()
    n_ohlc = ohlc_capture.capture_eod_ohlc(today, primary, fetch_live_parallel)
    state["last_ohlc_capture_date"] = today.isoformat()
    state["ohlc_bars_total"] = n_ohlc
    print(f"   captured OHLC for {n_ohlc} stocks  → ./ohlc/")

    # Refresh the corporate-event calendar so tomorrow's entries see the
    # latest dividends/earnings. The cache itself has a 24h staleness check
    # so calling refresh once per day from the EOD sweep is the natural fit.
    events_cache = calendar_cache.load_cache()
    if calendar_cache.is_stale(events_cache):
        n = len(primary)
        print(f"   refreshing event calendar for {n} stocks…")
        calendar_cache.refresh_cache([t["insref"] for t in primary])
        print("   event calendar refreshed")
    else:
        print("   event calendar still fresh, skipping refresh")

    state["last_eod_sweep_date"] = today.isoformat()
    save_state(state)


# ----------------------------------------------------------------------------- main

def main():
    # Trim the log BEFORE any print so the current run's output isn't part of
    # what we measure. Safe to do here: launchd runs one instance at a time,
    # and we haven't written anything yet.
    rotate_log()

    state = load_state()
    now = datetime.now(timezone.utc)
    today = now.date()
    now_local_ts = datetime.now().replace(microsecond=0).isoformat()  # 2026-05-19T10:23:45

    print("=" * 72)
    print(f" Live signal run @ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} local")
    print("=" * 72)

    if not is_market_open():
        if is_in_eod_window() and state.get("last_eod_sweep_date") != today.isoformat():
            run_eod_sweep(state, today, now_local_ts)
        else:
            print(" Market closed (Stockholm OMX 09:00-17:30 Mon-Fri). Skipping.")
        return

    # ---------- 0. Probe the data feed lag (one trades call on SEB A) + read
    #              S&P 500 regime context (no API call — local History.csv)
    lag = probe_data_lag()
    state["data_lag"] = lag
    state["us_context"] = read_spx_context()
    us = state.get("us_context")
    if us:
        stale = f" [STALE {us['stale_days']}d]" if us["stale_days"] > 3 else ""
        print(f" US tape (SPX {us['date']}): {us['return_pct']:+.2f}%{stale}")
    if lag.get("lag_seconds") is not None:
        ls = lag["lag_seconds"]
        mins = ls // 60
        secs = ls % 60
        flag = " [DELAYED FEED]" if ls >= 600 else ""
        print(f" Data lag: {mins}m {secs}s behind real-time  "
              f"(last {lag['ticker']} trade at {lag['trade_dt'][11:19]}){flag}")
    elif lag.get("error"):
        print(f" Data lag: probe failed ({lag['error']})")

    # ---------- 1. Build universe + log changes
    primary, watchlist = build_universe()
    approved_set     = load_approved()
    force_entry_set  = load_force_entries()
    # Promote user-approved watchlist tickers into the tradable scan; flag them
    # so logs and persisted signal_info can distinguish their origin.
    approved_promoted = [dict(t, from_watchlist=True) for t in watchlist if t["name"] in approved_set]
    watchlist_remaining = [t for t in watchlist if t["name"] not in approved_set]
    tradable = primary + approved_promoted

    primary_names = sorted(t["name"] for t in primary)
    watchlist_names = sorted(t["name"] for t in watchlist)
    prev = set(state.get("last_universe", []))
    cur  = set(primary_names)
    added   = sorted(cur - prev)
    removed = sorted(prev - cur)

    print(f" Universe: {len(primary)} large cap (tradable)  |  "
          f"{len(watchlist)} mid+small cap (watchlist only)  |  "
          f"{len(approved_promoted)} watchlist approved for trading")
    if added:
        print(f"   + added to primary:   {', '.join(added)}")
    if removed:
        print(f"   - removed from primary: {', '.join(removed)}")
    if approved_promoted:
        print(f"   ✓ approved watchlist: {', '.join(sorted(t['name'] for t in approved_promoted))}")

    # Build/refresh the ticker -> stock-page rel_path map used by the UI for
    # click-through to the stock detail page. Persisted on state and merged with
    # the prior map so closed-trade tickers no longer in the live universe still
    # resolve. rel_path matches what `/view?path=...` (app.py) expects.
    ticker_paths = state.get("ticker_paths", {}) or {}
    for t in primary + watchlist:
        if t.get("folder"):
            ticker_paths[t["name"]] = f"Equity/SEK/{t['folder']}"
    for tk, pos in state["positions"].items():
        if tk in ticker_paths:
            continue
        insref = pos.get("insref")
        if insref:
            folder, _ = _find_history_csv(insref)
            if folder:
                ticker_paths[tk] = f"Equity/SEK/{folder}"
    state["ticker_paths"] = ticker_paths

    # ---------- 2. Live quotes for tradable universe (parallel) — primary + approved
    primary_by_name = {t["name"]: t for t in tradable}
    live = fetch_live_parallel(tradable)
    for name, t in primary_by_name.items():
        lq = live.get(name, {"ok": False})
        if not lq.get("ok") or lq.get("last") in (None, 0):
            rows = load_history_closes(t["csv"])
            if rows:
                lq = {"last": rows[-1][1], "ask": rows[-1][1], "bid": rows[-1][1],
                      "ok": True, "source": "csv-fallback"}
        else:
            lq["source"] = "live"
        live[name] = lq

    # Also fetch live for any open positions whose ticker fell out of universe
    for tk in state["positions"]:
        if tk in live:
            continue
        # Find this ticker's insref from the position itself (we stored it on entry)
        insref = state["positions"][tk].get("insref")
        if insref:
            live[tk] = get_live(insref)
            if not live[tk].get("ok"):
                live[tk] = {"last": state["positions"][tk]["entry_price"], "ok": True,
                            "source": "stale"}
            else:
                live[tk]["source"] = "live"

    # ---------- 3. Exit checks on open positions
    force_sells = load_force_sells()
    closed_now  = apply_exit_checks(state, today, now_local_ts, live, force_sells)
    # Remove any tickers actually sold from the queue (one-shot semantics).
    consume_force_sells({t["ticker"] for t in closed_now if t["reason"] == "MANUAL"})

    # ---------- 4. Scan primary universe — first gather candidates, then rank
    candidates = []
    primary_filtered_info = []
    for t in tradable:
        name = t["name"]
        if name in state["positions"]:
            continue
        lq = live.get(name)
        if not lq or lq.get("last") is None:
            continue
        rows = load_history_closes(t["csv"])
        if len(rows) < 30:
            continue

        last_row_d = datetime.fromtimestamp(rows[-1][0] / 1000, tz=timezone.utc).date()
        live_close = lq["last"]
        closes = ([r[1] for r in rows[:-1]] + [live_close]) if last_row_d == today \
                 else ([r[1] for r in rows] + [live_close])

        ok, info = signal_check(closes)
        if t.get("from_watchlist"):
            info["from_watchlist"] = True
        # User-approved force entry: override the filter and take the trade
        # anyway (one-shot — consumed on entry, like watchlist approvals).
        if name in force_entry_set and "err" not in info:
            info["forced"]     = True
            info["entry_tag"]  = "forced"
            info["conviction"] = "normal"
            ok = True
        if not ok:
            if "err" not in info:
                primary_filtered_info.append((name, info))
            continue
        candidates.append({"t": t, "lq": lq, "info": info, "live_close": live_close})

    # Rank: strongest signal first (most-negative DoD = biggest down day)
    candidates.sort(key=lambda c: c["info"].get("dod", 0.0))

    # Cooldown lookup: most-recent exit date + reason per ticker. Two rules:
    #  - SL exit  -> block re-entry for COOLDOWN_DAYS trading days
    #  - TP / TIMEOUT exit -> block re-entry on the same calendar day only
    #    (next trading day is fine — the signal is allowed to re-fire on a
    #    fresh bar, but not on the same live tick that triggered the exit)
    # Includes exits closed earlier in *this same run* (already appended to
    # closed_trades by apply_exit_checks), so an SL/TP at 15:19 cannot be
    # immediately re-bought at 15:19.
    recent_exits = {}   # ticker -> (exit_date_str, reason)
    for tr in state.get("closed_trades", []):
        ex = tr.get("exit_date")
        if not ex:
            continue
        cur = recent_exits.get(tr["ticker"])
        if cur is None or ex > cur[0]:
            recent_exits[tr["ticker"]] = (ex, tr.get("reason"))

    # Dedupe by company stem so we don't take A and B of the same name
    held_stems = {company_stem(tk) for tk in state["positions"]}
    new_signals = []
    consumed_approvals    = set()
    consumed_force_entries = set()
    # Load the corporate-event cache once per run — used to block entries
    # near ex-dividend dates / earnings reports (added 2026-05-25 after
    # Tele2 A was bought into a 6-days-stale ex-dividend tick).
    events_cache = calendar_cache.load_cache()
    for c in candidates:
        t, lq, info, live_close = c["t"], c["lq"], c["info"], c["live_close"]
        name = t["name"]
        stem = company_stem(name)
        if stem in held_stems:
            new_signals.append({"ticker": name, "skipped": f"duplicate of held '{stem}'"})
            continue
        blocked, why = calendar_cache.has_nearby_event(t["insref"], today, cache=events_cache)
        if blocked:
            # Force-entry override still respected — user can bypass the
            # event filter by explicitly approving the near-miss.
            if name not in force_entry_set:
                new_signals.append({"ticker": name, "skipped": f"event window: {why}"})
                continue
        last = recent_exits.get(name)
        if last:
            last_date, last_reason = last
            last_d = (datetime.fromisoformat(last_date).date()
                      if isinstance(last_date, str) else last_date)
            if last_reason == "SL":
                tds = trading_days_between(last_d, today)
                if tds < COOLDOWN_DAYS:
                    new_signals.append({"ticker": name,
                                        "skipped": f"cooldown {tds}d/{COOLDOWN_DAYS}d since SL {last_date}"})
                    continue
            elif last_d >= today:
                new_signals.append({"ticker": name,
                                    "skipped": f"same-day re-entry blocked (just exited {last_reason} {last_date})"})
                continue
        if state["cash"] < POS_MIN:
            new_signals.append({"ticker": name, "skipped": f"low cash {state['cash']:.0f}"})
            continue
        entry_price = lq.get("ask") or live_close
        conviction = info.get("conviction", "normal")
        target, cap = CONVICTION_SIZING[conviction]
        size = min(target, state["cash"], cap)
        shares = int(size // entry_price)
        if shares <= 0 or shares * entry_price < POS_MIN:
            new_signals.append({"ticker": name, "skipped": f"size <{POS_MIN/1000:.0f}k after rounding"})
            continue

        cost     = shares * entry_price
        tp_price = round(entry_price * (1 + TP_PCT), 4)
        sl_price = round(entry_price * (1 + SL_PCT), 4)
        exit_by  = add_trading_days(today, MAX_HOLD_DAYS)

        state["cash"] -= cost
        state["positions"][name] = {
            "insref":           t["insref"],
            "entry_price":      entry_price,
            "shares":           shares,
            "entry_date":       today.isoformat(),
            "entry_ts":         now_local_ts,
            "tp":               tp_price,
            "sl":               sl_price,
            "target_exit_date": exit_by.isoformat(),
            "cost_sek":         cost,
            "signal_info":      info,
        }
        held_stems.add(stem)
        if t.get("from_watchlist"):
            consumed_approvals.add(name)
        if info.get("forced"):
            consumed_force_entries.add(name)
        new_signals.append({
            "ticker":   name,
            "entry":    entry_price,
            "tp":       tp_price,
            "sl":       sl_price,
            "shares":   shares,
            "cost":     cost,
            "exit_by":  exit_by.isoformat(),
            "info":     info,
        })

    if consumed_approvals:
        consume_approvals(consumed_approvals)
        approved_set = approved_set - consumed_approvals
        print(f"   ✗ consumed approvals (re-approve to trade again): "
              f"{', '.join(sorted(consumed_approvals))}")
    if consumed_force_entries:
        consume_force_entries(consumed_force_entries)
        force_entry_set = force_entry_set - consumed_force_entries
        print(f"   ✗ consumed force-entries (re-approve to override again): "
              f"{', '.join(sorted(consumed_force_entries))}")

    # ---------- 5. Watchlist scan (unapproved mid/small cap, CSV-only, no entries)
    # Approved tickers are scanned in step 4 via the live-quote path; everything
    # else surfaces here as monitor-only so the user can decide to approve it.
    watchlist_hits = []
    for t in watchlist_remaining:
        rows = load_history_closes(t["csv"])
        if len(rows) < 30:
            continue
        closes = [r[1] for r in rows]
        ok, info = signal_check(closes)
        if ok:
            watchlist_hits.append({
                "ticker": t["name"],
                "insref": t["insref"],
                "last":   closes[-1],
                "info":   info,
            })

    # ---------- 6. Reporting
    open_value = 0.0
    for tk, pos in state["positions"].items():
        cur_price = live.get(tk, {}).get("last") or pos["entry_price"]
        open_value += pos["shares"] * cur_price
    equity = state["cash"] + open_value
    pnl_realized = sum(t["pnl_sek"] for t in state["closed_trades"])

    print(f"\nWallet:    cash {state['cash']:>10,.0f}   open {open_value:>10,.0f}   "
          f"equity {equity:>10,.0f}  SEK")
    print(f"P&L:       realized {pnl_realized:+,.0f} SEK   "
          f"open positions {len(state['positions'])}   "
          f"closed trades {len(state['closed_trades'])}")

    if closed_now:
        print(f"\n--- CLOSED THIS RUN ({len(closed_now)}) ---")
        for t in closed_now:
            tag_str = format_signal_tag(t.get("signal_info"))
            print(f"  {tag_str:<10} {t['ticker']:<22} {t['reason']:<7} @ {t['exit_price']:.2f}   "
                  f"P&L {t['pnl_sek']:+,.0f} SEK ({t['ret_pct']*100:+.2f}%)   "
                  f"opened {t['entry_date']}")

    entered = [s for s in new_signals if "skipped" not in s]
    skipped = [s for s in new_signals if "skipped" in s]
    if entered or skipped:
        print(f"\n--- NEW SIGNALS ({len(entered)} entered, {len(skipped)} skipped) ---")
        for s in entered:
            tag_str = format_signal_tag(s["info"])
            print(f"  BUY {tag_str:<10} {s['ticker']:<18}  {s['shares']:>5} sh @ {s['entry']:.2f}  "
                  f"= {s['cost']:>9,.0f} SEK")
            print(f"      target exit {s['tp']:.2f} (+{TP_PCT*100:.2f}%)   "
                  f"stop {s['sl']:.2f} ({SL_PCT*100:+.2f}%)   "
                  f"exit by {s['exit_by']}")
            i = s["info"]
            print(f"      signal:  DoD {i['dod']*100:+.2f}%   "
                  f"20d ret {i['ret_20d']*100:+.1f}%")
        for s in skipped[:3]:
            print(f"  skipped: {s['ticker']:<22} ({s['skipped']})")
        if len(skipped) > 3:
            print(f"  ... +{len(skipped) - 3} more skipped")
    else:
        print("\n--- NO NEW SIGNALS IN PRIMARY UNIVERSE ---")

    if watchlist_hits:
        print(f"\n--- WATCHLIST SIGNALS ({len(watchlist_hits)}) — monitor only, do NOT trade ---")
        for w in watchlist_hits[:20]:
            i = w["info"]
            tag_str = format_signal_tag(i)
            print(f"  {tag_str:<10} {w['ticker']:<28} last {w['last']:>7.2f}   "
                  f"DoD {i['dod']*100:+.2f}%   "
                  f"20d {i['ret_20d']*100:+.1f}%")
        if len(watchlist_hits) > 20:
            print(f"  ... +{len(watchlist_hits) - 20} more")

    if state["positions"]:
        print(f"\n--- OPEN POSITIONS ({len(state['positions'])}) ---")
        for tk, pos in state["positions"].items():
            cur_price = live.get(tk, {}).get("last") or pos["entry_price"]
            pnl = pos["shares"] * (cur_price - pos["entry_price"])
            ret = cur_price / pos["entry_price"] - 1
            tag_str = format_signal_tag(pos.get("signal_info"))
            print(f"  {tag_str:<10} {tk:<22} {pos['shares']:>5} sh   "
                  f"entered {pos['entry_date']} @ {pos['entry_price']:.2f}   "
                  f"now {cur_price:.2f}   P&L {pnl:+,.0f} SEK ({ret*100:+.2f}%)")
            print(f"                          TP {pos['tp']:.2f}   SL {pos['sl']:.2f}   "
                  f"exit by {pos['target_exit_date']}")

    # Compact diagnostic: closest-to-passing in primary universe
    def closeness(info):
        # smaller = closer to passing
        dod_gap   = max(0.0, info.get("dod", 0.0) - DOD_THRESHOLD)   # need dod <= -1%
        hi5_gap   = 0.4 if info.get("at_5d_high") else 0.0           # binary penalty
        fri_gap   = 0.3 if info.get("is_friday") else 0.0
        crash_gap = max(0.0, CRASH_GUARD - info.get("ret_20d", 0.0))
        return dod_gap + hi5_gap + fri_gap + crash_gap

    def fails_for(info):
        checks = []
        if info.get("dod", 0.0) > DOD_THRESHOLD:
            checks.append(f"DoD:{info['dod']*100:+.2f}%")
        if info.get("at_5d_high"):
            checks.append("at 5d high")
        if info.get("is_friday"):
            checks.append("Friday")
        if info.get("ret_20d", 0.0) <= CRASH_GUARD:
            checks.append(f"20d:{info['ret_20d']*100:+.1f}%")
        return checks

    ranked_near_misses = sorted(primary_filtered_info, key=lambda x: closeness(x[1]))
    if ranked_near_misses:
        print(f"\n--- CLOSEST TO SIGNAL (top 5 of {len(ranked_near_misses)} primary) ---")
        for name, info in ranked_near_misses[:5]:
            checks = fails_for(info)
            print(f"  {name:<26} fails: {', '.join(checks) if checks else 'borderline'}")

    state["last_universe"] = primary_names
    state["last_watchlist"] = watchlist_names
    state["last_watchlist_hits"] = watchlist_hits
    state["approved_watchlist"] = sorted(approved_set)
    state["force_entry_approved"] = sorted(force_entry_set)

    # Top 10 near-misses persisted for the UI's "approve to override" panel.
    # Same `info` dict the candidate had at signal_check time, plus a fails
    # list naming which rule(s) it missed.
    state["last_near_misses"] = [
        {"ticker": name, "info": info, "fails": fails_for(info)}
        for name, info in ranked_near_misses[:10]
    ]

    # ---------- 7. Snapshot quotes + recent signals for the UI
    interesting = set(state["positions"].keys())
    for s in new_signals:
        interesting.add(s["ticker"])
    for w in watchlist_hits:
        interesting.add(w["ticker"])

    last_quotes = {}
    for name in interesting:
        lq = live.get(name)
        if not lq:
            continue
        last_quotes[name] = {
            "last":     lq.get("last"),
            "bid":      lq.get("bid"),
            "ask":      lq.get("ask"),
            "diff_pct": lq.get("diffprc"),
            "source":   lq.get("source"),
        }
    state["last_quotes"] = last_quotes

    def _mid(b, a):
        return round((b + a) / 2, 4) if isinstance(b, (int, float)) and isinstance(a, (int, float)) else None

    signal_events = []
    ts_iso = now.isoformat()
    for s in new_signals:
        name = s["ticker"]
        lq = live.get(name, {})
        bid, ask, last = lq.get("bid"), lq.get("ask"), lq.get("last")
        mid = _mid(bid, ask)
        spread = round(ask - bid, 4) if isinstance(bid, (int, float)) and isinstance(ask, (int, float)) else None
        info = s.get("info") or {}
        evt = {
            "ts":            ts_iso,
            "ticker":        name,
            "action":        "skipped" if "skipped" in s else "entered",
            "skip_reason":   s.get("skipped"),
            "last":          last,
            "bid":           bid,
            "ask":           ask,
            "spread":        spread,
            "suggested_bid": mid,        # mid-price = patient compromise
            "entry":         s.get("entry"),
            "tp":            s.get("tp"),
            "sl":            s.get("sl"),
            "shares":        s.get("shares"),
            "cost":          s.get("cost"),
            "exit_by":       s.get("exit_by"),
            "dod":           info.get("dod"),
            "at_5d_high":    info.get("at_5d_high"),
            "is_friday":     info.get("is_friday"),
            "ret_20d":       info.get("ret_20d"),
        }
        signal_events.append(evt)
    state.setdefault("recent_signals", [])
    state["recent_signals"].extend(signal_events)
    state["recent_signals"] = state["recent_signals"][-50:]

    save_state(state)


if __name__ == "__main__":
    main()
    # After the mean-reversion engine finishes its 3-min cycle, run the
    # candlestick-pattern engine in the same process. Wrapped in try/except so
    # a bug in the newer patterns engine never breaks the validated live_signals
    # engine. Errors are printed to launchd's stdout (live_signals.log).
    try:
        import live_patterns
        live_patterns.main()
    except Exception as e:
        import traceback
        print("\n[live_patterns crash]")
        traceback.print_exc()
