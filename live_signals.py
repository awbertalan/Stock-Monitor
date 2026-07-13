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
  Cooldown:  SL/TP/TIMEOUT exits all block same-day re-entry only — re-entry
             allowed on the next trading day. (SL cooldown was 7 trading days
             pre-2026-06-13; reduced to 1 to maximise opportunity to re-enter
             after the volatility that triggered the stop has passed.)
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
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

import env_loader      # noqa: E402
env_loader.load_env()
import engine_common as ec  # noqa: E402
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
LIMIT_ORDERS_PATH = os.path.join(STATE_DIR, "live_limit_orders.json")
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
COOLDOWN_DAYS = 1       # block re-entry on a ticker for N trading days after an SL exit only (was 7, reduced 2026-06-13 to same-day-only block — re-enter next trading day)
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
    return ec.load_state(STATE_PATH, {
        "cash":              WALLET_START,
        "positions":         {},
        "closed_trades":     [],
        "last_run":          None,
        "last_universe":     [],
        "last_watchlist":    [],
    })


def save_state(state):
    ec.save_state(STATE_PATH, state)


def load_approved():
    """Load the list of watchlist tickers the user has approved for trading."""
    data = ec.read_json(APPROVED_PATH, [])
    return set(data) if isinstance(data, list) else set()


def consume_approvals(consumed):
    """Remove `consumed` tickers from the approved file. Re-reads first so any
    new approvals added via the web UI during the run aren't clobbered."""
    if not consumed:
        return
    remaining = load_approved() - set(consumed)
    ec.write_json_atomic(APPROVED_PATH, sorted(remaining), ensure_ascii=False)


def load_force_entries():
    """Tickers the user has approved for forced entry on the next signal check
    — bypasses the validated filter entirely. Use case: a near-miss the user
    wants to take anyway (e.g. DoD -0.9% instead of the required -1.0%).
    One-shot — consumed on entry, like watchlist approvals."""
    data = ec.read_json(FORCE_ENTRY_PATH, [])
    return set(data) if isinstance(data, list) else set()


def consume_force_entries(consumed):
    if not consumed:
        return
    remaining = load_force_entries() - set(consumed)
    ec.write_json_atomic(FORCE_ENTRY_PATH, sorted(remaining), ensure_ascii=False)


def load_force_sells(path=None):
    """Tickers the user has queued for an immediate market sell on the next
    engine run. Bypasses TP/SL/TIMEOUT — exits at the current live price with
    reason='MANUAL'. One-shot: consumed once the exit is recorded.

    `path` defaults to the Live Signals queue but the same helper is reused
    by live_patterns.py with its own per-engine path."""
    p = path or FORCE_SELL_PATH
    data = ec.read_json(p, [])
    return set(data) if isinstance(data, list) else set()


def consume_force_sells(consumed, path=None):
    if not consumed:
        return
    p = path or FORCE_SELL_PATH
    remaining = load_force_sells(p) - set(consumed)
    ec.write_json_atomic(p, sorted(remaining), ensure_ascii=False)


def load_limit_orders():
    """User-placed buy limit orders for primary signals (manual approve+price
    flow). Dict keyed by ticker:
        {ticker: {insref, limit_price, placed_date, placed_ts, info,
                  from_watchlist, forced}}
    Each run the engine fills an order at its limit_price once the live price
    trades at/below it. Orders are good-for-the-day only — any whose
    placed_date != today are expired (see consume_limit_orders in main())."""
    data = ec.read_json(LIMIT_ORDERS_PATH, {})
    return data if isinstance(data, dict) else {}


def save_limit_orders(orders):
    ec.write_json_atomic(LIMIT_ORDERS_PATH, orders, ensure_ascii=False, default=str)


def consume_limit_orders(remove):
    """Remove `remove` tickers (filled + expired + cancelled) from the queue.
    Re-reads first so any order the UI placed mid-run isn't clobbered."""
    if not remove:
        return
    fresh = load_limit_orders()
    for tk in remove:
        fresh.pop(tk, None)
    save_limit_orders(fresh)


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
    """Trim live_signals.log to the last LOG_RETAIN_HOURS of output. Called at
    the top of main()/EOD-sweep before any print, so there's no race with our
    own stdout. See engine_common.rotate_log for the truncation mechanism."""
    ec.rotate_log(LOG_PATH, " Live signal run @ ", LOG_RETAIN_HOURS, LOG_ROTATE_MIN_BYTES)


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
        lq = live.get(tk, {})
        cur_price = lq.get("last")
        if cur_price is None:
            continue
        # csv-fallback / stale = the live fetch failed and we substituted a
        # synthetic price (last CSV row or entry price). Never trigger price-
        # based exits on a non-live quote — wait for the next good run.
        if lq.get("source") in ("csv-fallback", "stale"):
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


def maybe_capture_eod_ohlc(state, today):
    """Self-gated daily OHLC capture, decoupled from run_eod_sweep().

    Runs at most once per trading day. Safe to call from every launchd tick
    after market close — idempotent on `state["last_ohlc_capture_date"]`,
    and capture_eod_ohlc() itself dedupes today's row inside each per-stock
    CSV. Decoupled from run_eod_sweep so an empty `state["positions"]`
    doesn't block capture (that early-return at run_eod_sweep:693-697 is
    what kept capture from ever firing pre-2026-06-13).

    Gating:
      · skip if already captured today
      · skip on weekends (no Stockholm close to capture)
      · skip if local clock is still before MARKET_CLOSE (17:30)
      · skip if the feed probe (SEB A trade timestamp) fails outright
      · skip if the feed trade is from a different date than `today` —
        guards against writing today's bar using yesterday's OHLC fields
        when the feed hasn't yet advanced past the previous close
      · skip if the feed is still before today's 17:30 close
    Any skip leaves `last_ohlc_capture_date` unset so the next tick retries.

    Any weekday tick after 17:30 (inside or outside the EOD window) will
    capture once the feed has settled. Missing the narrow 17:45-18:30
    window no longer means missing the day.
    """
    if state.get("last_ohlc_capture_date") == today.isoformat():
        return

    now_local = datetime.now()
    if now_local.weekday() >= 5:
        return
    mins_now = now_local.hour * 60 + now_local.minute
    close_mins = MARKET_CLOSE[0] * 60 + MARKET_CLOSE[1]
    if mins_now < close_mins:
        return

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
        print(f" OHLC capture: feed probe failed ({lag.get('error','unknown')}); retry next run")
        return
    if trade_dt.date() != today:
        print(f" OHLC capture: feed still on {trade_dt.date().isoformat()} "
              f"(today {today.isoformat()}); retry next run")
        return
    close_time = trade_dt.replace(hour=MARKET_CLOSE[0], minute=MARKET_CLOSE[1],
                                  second=0, microsecond=0)
    if trade_dt < close_time:
        gap = int((close_time - trade_dt).total_seconds() // 60)
        print(f" OHLC capture: feed only at {trade_dt.strftime('%H:%M:%S')} "
              f"({gap}m before close); retry next run")
        return

    primary, _ = build_universe()
    n_ohlc = ohlc_capture.capture_eod_ohlc(today, primary, fetch_live_parallel)
    state["last_ohlc_capture_date"] = today.isoformat()
    state["ohlc_bars_total"] = n_ohlc
    print(f" OHLC capture: wrote {n_ohlc} bars for {today.isoformat()} → ./ohlc/")
    save_state(state)


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

    # OHLC capture moved to maybe_capture_eod_ohlc() — it now runs as a
    # self-gated daily step in main(), independent of positions being open
    # and of the narrow 17:45-18:30 EOD window. We still build the universe
    # here so the calendar refresh below has the primary insref list.
    primary, _ = build_universe()

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
    today = ec.today_local()   # local Stockholm trade date — must agree with the
                               # Friday / market-hours gates (feed is market-local)
    now_local_ts = datetime.now().replace(microsecond=0).isoformat()  # 2026-05-19T10:23:45

    print("=" * 72)
    print(f" Live signal run @ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} local")
    print("=" * 72)

    if not is_market_open():
        # OHLC capture is independent of the EOD sweep — runs on any post-close
        # weekday tick (idempotent via last_ohlc_capture_date). Catches days
        # when the 17:45-18:30 window was missed (Mac asleep, feed late, etc.)
        # and days with no open positions (where run_eod_sweep early-returns
        # before what used to be the capture step). Called first so its feed
        # probe also primes state['data_lag'] for the EOD sweep that follows.
        try:
            maybe_capture_eod_ohlc(state, today)
        except Exception as e:
            print(f" OHLC capture: unexpected error: {e!r}")
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
    fallback_names = []
    for name, t in primary_by_name.items():
        lq = live.get(name, {"ok": False})
        if not lq.get("ok") or lq.get("last") in (None, 0):
            rows = load_history_closes(t["csv"])
            if rows:
                lq = {"last": rows[-1][1], "ask": rows[-1][1], "bid": rows[-1][1],
                      "ok": True, "source": "csv-fallback"}
                fallback_names.append(name)
        else:
            lq["source"] = "live"
        live[name] = lq
    if fallback_names:
        print(f"   API fetch failed for {len(fallback_names)} ticker(s); "
              f"using csv-fallback for scan only (UI/exits skip these): "
              f"{', '.join(fallback_names[:10])}"
              f"{' …' if len(fallback_names) > 10 else ''}")

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
                primary_filtered_info.append(
                    {"name": name, "info": info, "insref": t["insref"], "last": live_close})
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
    recent_exits = ec.build_recent_exits(state)   # ticker -> (exit_date_str, reason)

    # Dedupe by company stem so we don't take A and B of the same name
    held_stems = {company_stem(tk) for tk in state["positions"]}
    pending         = []   # candidates that passed every gate — await user approve+price
    skipped_signals = []   # informational skips surfaced in the UI
    consumed_approvals    = set()
    consumed_force_entries = set()
    # Load the corporate-event cache once per run — used to block entries
    # near ex-dividend dates / earnings reports (added 2026-05-25 after
    # Tele2 A was bought into a 6-days-stale ex-dividend tick).
    events_cache = calendar_cache.load_cache()
    # MANUAL-ONLY flow (2026-05-27): primary signals no longer auto-enter. Each
    # candidate that clears every gate is surfaced as a *pending* signal; the
    # user approves it and sets a buy limit price in the UI, which becomes a
    # same-day limit order (filled in section 4b below when live <= limit).
    for c in candidates:
        t, lq, info, live_close = c["t"], c["lq"], c["info"], c["live_close"]
        name = t["name"]
        stem = company_stem(name)
        if stem in held_stems:
            skipped_signals.append({"ticker": name, "skipped": f"duplicate of held '{stem}'"})
            continue
        blocked, why = calendar_cache.has_nearby_event(t["insref"], today, cache=events_cache)
        if blocked:
            # Force-entry override still respected — user can bypass the
            # event filter by explicitly approving the near-miss.
            if name not in force_entry_set:
                skipped_signals.append({"ticker": name, "skipped": f"event window: {why}"})
                continue
        cd = ec.cooldown_status(recent_exits, name, today, COOLDOWN_DAYS, trading_days_between)
        if cd:
            if cd["kind"] == "SL":
                skipped_signals.append({"ticker": name,
                                        "skipped": f"cooldown {cd['tds']}d/{COOLDOWN_DAYS}d since SL {cd['last_date']}"})
            else:
                skipped_signals.append({"ticker": name,
                                        "skipped": f"same-day re-entry blocked (just exited {cd['last_reason']} {cd['last_date']})"})
            continue
        if state["cash"] < POS_MIN:
            skipped_signals.append({"ticker": name, "skipped": f"low cash {state['cash']:.0f}"})
            continue
        suggested = lq.get("ask") or live_close
        pending.append({
            "ticker":          name,
            "insref":          t["insref"],
            "last":            lq.get("last"),
            "bid":             lq.get("bid"),
            "ask":             lq.get("ask"),
            "suggested_price": suggested,
            "tp_preview":      round(suggested * (1 + TP_PCT), 4) if suggested else None,
            "sl_preview":      round(suggested * (1 + SL_PCT), 4) if suggested else None,
            "info":            info,
            "from_watchlist":  bool(t.get("from_watchlist")),
            "forced":          bool(info.get("forced")),
        })

    state["pending_signals"] = pending

    # ---------- 4b. Fill user-placed buy limit orders (manual approve+price)
    # Good-for-the-day: any order whose placed_date != today is expired. A live
    # order fills at its limit_price once the market trades at/below it. Sizing,
    # stem-dedup, cooldown and event gates were already applied when the
    # candidate was surfaced, but cash + stem are re-checked at fill time.
    orders = load_limit_orders()
    orders_to_remove = {tk for tk, o in orders.items()
                        if o.get("placed_date") != today.isoformat()}
    filled_now = []
    for tk, o in orders.items():
        if tk in orders_to_remove:
            continue
        if tk in state["positions"]:
            orders_to_remove.add(tk)          # already holding — clear the order
            continue
        lq = live.get(tk)
        if (not lq or lq.get("last") is None) and o.get("insref"):
            q = get_live(o["insref"])         # ticker not in scanned universe — fetch on demand
            if q.get("ok"):
                lq = q
        market = lq.get("last") if lq else None
        if market is None:
            continue                          # no price yet — leave the order pending
        limit_price = o.get("limit_price")
        if not isinstance(limit_price, (int, float)) or limit_price <= 0:
            orders_to_remove.add(tk)          # malformed — drop
            continue
        if market > limit_price:
            continue                          # limit not reached — stay pending
        stem = company_stem(tk)
        if stem in held_stems:
            orders_to_remove.add(tk)          # took a sibling share class — cancel
            continue
        if state["cash"] < POS_MIN:
            continue                          # can't afford yet — keep waiting for cash
        # Carry the order's source attribution onto the position's signal_info so
        # the UI tag + entry-source analytics (auto / watchlist / forced) are right.
        info = dict(o.get("info") or {})
        if o.get("from_watchlist"):
            info["from_watchlist"] = True
        if o.get("forced"):
            info["forced"] = True
            info["entry_tag"] = "forced"
        info.setdefault("conviction", "normal")
        conviction = info.get("conviction", "normal")
        target, cap = CONVICTION_SIZING.get(conviction, CONVICTION_SIZING["normal"])
        shares, cost = ec.position_size(state["cash"], limit_price, target, cap, POS_MIN)
        if shares <= 0:
            orders_to_remove.add(tk)          # can't make the min size at this price — drop
            continue
        tp_price = round(limit_price * (1 + TP_PCT), 4)
        sl_price = round(limit_price * (1 + SL_PCT), 4)
        exit_by  = add_trading_days(today, MAX_HOLD_DAYS)
        state["cash"] -= cost
        state["positions"][tk] = {
            "insref":           o.get("insref") or (lq.get("insref") if lq else None),
            "entry_price":      limit_price,
            "shares":           shares,
            "entry_date":       today.isoformat(),
            "entry_ts":         now_local_ts,
            "tp":               tp_price,
            "sl":               sl_price,
            "target_exit_date": exit_by.isoformat(),
            "cost_sek":         cost,
            "signal_info":      info,
            "fill_type":        "limit",
        }
        held_stems.add(stem)
        orders_to_remove.add(tk)
        if o.get("from_watchlist"):
            consumed_approvals.add(tk)
        if o.get("forced"):
            consumed_force_entries.add(tk)
        filled_now.append({
            "ticker":  tk, "entry": limit_price, "limit": limit_price, "market": market,
            "tp": tp_price, "sl": sl_price, "shares": shares, "cost": cost,
            "exit_by": exit_by.isoformat(), "info": info,
        })

    consume_limit_orders(orders_to_remove)

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

    if filled_now:
        print(f"\n--- LIMIT ORDERS FILLED THIS RUN ({len(filled_now)}) ---")
        for s in filled_now:
            tag_str = format_signal_tag(s["info"])
            print(f"  FILL {tag_str:<10} {s['ticker']:<18}  {s['shares']:>5} sh @ {s['entry']:.2f}  "
                  f"= {s['cost']:>9,.0f} SEK  (limit {s['limit']:.2f}, market {s['market']:.2f})")
            print(f"      target exit {s['tp']:.2f} (+{TP_PCT*100:.2f}%)   "
                  f"stop {s['sl']:.2f} ({SL_PCT*100:+.2f}%)   "
                  f"exit by {s['exit_by']}")

    open_orders = load_limit_orders()
    if open_orders:
        print(f"\n--- OPEN LIMIT ORDERS ({len(open_orders)}) — awaiting fill ---")
        for tk, o in sorted(open_orders.items()):
            mk = live.get(tk, {}).get("last")
            mk_str = f"{mk:.2f}" if isinstance(mk, (int, float)) else "?"
            print(f"  {tk:<22} limit {o.get('limit_price', 0):.2f}   market {mk_str}   placed {o.get('placed_date')}")

    if pending or skipped_signals:
        print(f"\n--- NEW SIGNALS ({len(pending)} pending approval, {len(skipped_signals)} skipped) ---")
        for s in pending:
            tag_str = format_signal_tag(s["info"])
            i = s["info"]
            print(f"  PENDING {tag_str:<10} {s['ticker']:<18}  last {s['last'] if s['last'] is not None else '?'}   "
                  f"suggested {s['suggested_price']:.2f}   "
                  f"DoD {i['dod']*100:+.2f}%   20d {i['ret_20d']*100:+.1f}%")
        for s in skipped_signals[:3]:
            print(f"  skipped: {s['ticker']:<22} ({s['skipped']})")
        if len(skipped_signals) > 3:
            print(f"  ... +{len(skipped_signals) - 3} more skipped")
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

    ranked_near_misses = sorted(primary_filtered_info, key=lambda x: closeness(x["info"]))
    if ranked_near_misses:
        print(f"\n--- CLOSEST TO SIGNAL (top 5 of {len(ranked_near_misses)} primary) ---")
        for nm in ranked_near_misses[:5]:
            checks = fails_for(nm["info"])
            print(f"  {nm['name']:<26} fails: {', '.join(checks) if checks else 'borderline'}")

    state["last_universe"] = primary_names
    state["last_watchlist"] = watchlist_names
    state["last_watchlist_hits"] = watchlist_hits
    state["approved_watchlist"] = sorted(approved_set)
    state["force_entry_approved"] = sorted(force_entry_set)

    # Top 10 near-misses persisted for the UI's "approve to override" panel.
    # Same `info` dict the candidate had at signal_check time, plus insref +
    # last price (so the user can place a buy limit order on it) and a fails
    # list naming which rule(s) it missed.
    state["last_near_misses"] = [
        {"ticker": nm["name"], "insref": nm["insref"], "last": nm["last"],
         "info": nm["info"], "fails": fails_for(nm["info"])}
        for nm in ranked_near_misses[:10]
    ]

    # ---------- 7. Snapshot quotes + recent signals for the UI
    interesting = set(state["positions"].keys())
    for s in pending:
        interesting.add(s["ticker"])
    for s in filled_now:
        interesting.add(s["ticker"])
    for tk in load_limit_orders():
        interesting.add(tk)
    for w in watchlist_hits:
        interesting.add(w["ticker"])

    # Build last_quotes from this run's live values, but never overwrite a
    # prior known-good quote with a csv-fallback / stale synthetic price —
    # that's how Securitas B ended up showing 154.20 in the UI while the
    # real close was 152.20. When the live fetch fails for a ticker, we
    # preserve the previous run's quote (re-tagged 'stale') so the UI keeps
    # showing the last real price instead of yesterday's CSV close.
    prior_quotes = state.get("last_quotes", {}) or {}
    state["last_quotes"] = ec.build_quote_snapshot(interesting, live, prior_quotes)

    def _mid(b, a):
        return round((b + a) / 2, 4) if isinstance(b, (int, float)) and isinstance(a, (int, float)) else None

    # Log this run's signal events for the UI / history. Manual-flow actions:
    #   "filled"  — a user limit order filled into a position this run
    #   "skipped" — candidate dropped by a gate (dedup/event/cooldown/cash)
    # Pending candidates awaiting approval are surfaced separately via
    # state["pending_signals"]; we don't log them here to avoid churn.
    signal_events = []
    ts_iso = now.isoformat()
    combined = ([{**s, "_action": "filled"}  for s in filled_now] +
                [{**s, "_action": "skipped"} for s in skipped_signals])
    for s in combined:
        name = s["ticker"]
        lq = live.get(name, {})
        bid, ask, last = lq.get("bid"), lq.get("ask"), lq.get("last")
        mid = _mid(bid, ask)
        spread = round(ask - bid, 4) if isinstance(bid, (int, float)) and isinstance(ask, (int, float)) else None
        info = s.get("info") or {}
        evt = {
            "ts":            ts_iso,
            "ticker":        name,
            "action":        s["_action"],
            "skip_reason":   s.get("skipped"),
            "last":          last,
            "bid":           bid,
            "ask":           ask,
            "spread":        spread,
            "suggested_bid": mid,        # mid-price = patient compromise
            "entry":         s.get("entry"),
            "limit":         s.get("limit"),
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
    # Cross engine runs LAST so it can read the fresh recent_pattern_fires
    # the patterns engine just wrote.
    try:
        import live_cross
        live_cross.main()
    except Exception as e:
        import traceback
        print("\n[live_cross crash]")
        traceback.print_exc()
