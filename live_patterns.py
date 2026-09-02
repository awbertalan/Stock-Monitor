"""
Live candlestick-pattern paper-trading engine — v1, launched 2026-05-25.

Mirrors live_signals.py's mean-reversion engine but uses candlestick patterns
as the entry trigger. Separate paper wallet, separate log, separate state file
to enable apples-to-apples A/B comparison with the mean-reversion engine.

Strategy
  Entry triggers: bullish candlestick patterns firing on yesterday's completed
                  daily OHLC bar
                    Bullish Engulfing | Morning Star | Piercing Line
                    Three White Soldiers | Tweezer Bottom
                    Hammer — DETECTED BUT ENTRY-DISABLED since 2026-08-18
                    (see ENTRY_DISABLED_PATTERNS)
  Filters:        not Friday entry · not after 16:30 local · ret_20d > -15%
                  · stem-dedup vs existing positions · SL cooldown 1d (same-day block) ·
                  TP/TIMEOUT same-day block
  Exits:          TP +2.0%, SL -2.0%, max 7-day hold (limit-fill capped via
                  apply_exit_checks reused from live_signals)
  Sizing:         single tier — target 70k, cap 85k, floor 35k against 500k SEK
  Universe:       OMX large cap (loaded from stock_names.csv)

OHLC source
  We aggregate the existing intraday *_7d.csv files (1-min closes) into daily
  OHLC bars. Gives us 5-7 daily bars per stock right now. Once the EOD OHLC
  capture under ohlc/ accumulates ~30+ days (early July 2026), the engine can
  be migrated to read from ohlc/{insref}_OHLC.csv for more accurate bars
  (the intraday file only has minute closes, not actual highs/lows of each
  minute, so the derived high/low is "max/min of minute closes" — close enough
  but not exact).

Run lifecycle
  Called from live_signals.main() after the mean-reversion run completes,
  inside the same 3-min LaunchAgent cadence. We use the same load_state/
  save_state/apply_exit_checks helpers as live_signals so cooldown, limit-fill
  capping, conviction sizing all behave identically.
"""

import csv
import glob
import os
import sys
from datetime import datetime, timezone
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

import env_loader                                    # noqa: E402
env_loader.load_env()
import engine_common as ec                           # noqa: E402
import live_signals as L                             # reuse helpers          # noqa: E402
import calendar_cache                                # noqa: E402
import version as V                                  # noqa: E402

ROOT       = os.path.dirname(os.path.abspath(__file__))
STATE_DIR  = os.path.join(ROOT, "state")
LOGS_DIR   = os.path.join(ROOT, "logs")
BASE       = os.path.join(ROOT, "Instrumenttype", "Equity", "SEK")
STATE_PATH      = os.path.join(STATE_DIR, "live_patterns_portfolio.json")
LOG_PATH        = os.path.join(LOGS_DIR,  "live_patterns.log")
FORCE_SELL_PATH = os.path.join(STATE_DIR, "live_patterns_force_sell.json")

WALLET_START  = 500_000.0
POS_MIN       =  35_000.0
TARGET_SIZE   =  70_000.0
CAP_SIZE      =  85_000.0

TP_PCT        =  0.020
SL_PCT        = -0.020   # -2.0% (matched to live_signals on 2026-05-25)
MAX_HOLD_DAYS = 7
COOLDOWN_DAYS = 1   # SL-exit re-entry block (reduced from 7 → 1 on 2026-06-13: same-day block only, next trading day allowed)
CRASH_GUARD   = -0.15
LATE_ENTRY_CUTOFF_MIN = 16 * 60 + 30   # 16:30 — no new entries after this

# Confluence gate (added 2026-06-13). When True, only enter if >= 2 distinct
# bullish patterns fire on the same bar. Bullish Engulfing was the only
# positively-validated pattern in the first 30 trades (+1,821 SEK n=8) while
# Hammer/Morning Star ran net-negative — confluence biases entries toward
# the higher-conviction setups. Flip to False to restore solo-pattern entries.
CONFLUENCE_REQUIRED   = True
CONFLUENCE_MIN_COUNT  = 2

# Patterns that still get DETECTED (so we keep collecting evidence on them) but
# may no longer open a position, and no longer count toward the confluence
# threshold above. Hammer was retired 2026-08-18 at n=9 / 22.2% WR / -6,637 SEK
# — a bigger loss than the whole wallet's drawdown — after the 2026-06-13 parity
# fixes failed to move it. Removing a name here re-enables it immediately.
ENTRY_DISABLED_PATTERNS = frozenset({"Hammer"})

# Block a name already held in the Signals or Cross wallet, collapsed to the
# company stem. This engine bought Electrolux A on 2026-08-24 while Signals held
# Electrolux B — 140k of one company across two books. Trade-off (the wallets
# stop being independent strategy samples) documented in engine_common.
CROSS_WALLET_DEDUPE = True

# Log retention (same in-place truncation trick as live_signals)
LOG_RETAIN_HOURS     = 24
LOG_ROTATE_MIN_BYTES = 200_000


# ── State persistence ────────────────────────────────────────────────────────

def load_state():
    return ec.load_state(STATE_PATH, {
        "cash":          WALLET_START,
        "wallet_start":  WALLET_START,
        "positions":     {},
        "closed_trades": [],
        "last_run":      None,
    })


def save_state(state):
    ec.save_state(STATE_PATH, state)


def rotate_log():
    """Trim live_patterns.log to the last 24h — see engine_common.rotate_log."""
    ec.rotate_log(LOG_PATH, " Live patterns run @ ", LOG_RETAIN_HOURS, LOG_ROTATE_MIN_BYTES)


# ── OHLC aggregation from intraday files ─────────────────────────────────────

def load_intraday_minute_closes(folder):
    """Find the *_7d.csv inside `folder` and return sorted [(ts_ms, price), …]."""
    candidates = glob.glob(os.path.join(folder, "*_7d.csv"))
    if not candidates:
        return []
    rows = []
    try:
        with open(candidates[0], encoding="utf-8") as f:
            for r in csv.reader(f):
                try:
                    rows.append((int(float(r[0])), float(r[1])))
                except (ValueError, IndexError):
                    pass
    except OSError:
        return []
    rows.sort()
    return rows


def aggregate_to_daily_ohlc(minute_rows):
    """Group 1-min closes into daily OHLC bars (UTC date buckets)."""
    by_day = defaultdict(list)
    for ts, p in minute_rows:
        d = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).date()
        by_day[d].append(p)
    bars = []
    for d in sorted(by_day.keys()):
        prices = by_day[d]
        bars.append({
            "date":  d,
            "open":  prices[0],
            "high":  max(prices),
            "low":   min(prices),
            "close": prices[-1],
        })
    return bars


# ── Pattern detectors (ported from the 2026-05-23 smoketest) ─────────────────

def _is_bull(b): return b["close"] > b["open"]
def _is_bear(b): return b["close"] < b["open"]
def _body(b):    return abs(b["close"] - b["open"])
def _range(b):   return max(b["high"] - b["low"], 1e-9)
def _upper(b):   return b["high"] - max(b["open"], b["close"])
def _lower(b):   return min(b["open"], b["close"]) - b["low"]


def _downtrend_before(bars, idx, prior_daily_closes):
    """Was the trend declining heading into bars[idx]?

    bars                  — list of dicts with at least 'date' + 'close'
    idx                   — index in `bars` we're checking
    prior_daily_closes    — {date: close} from the daily *_History.csv for the
                            wider trend context (older than what's in `bars`)
    """
    closes = [bars[j]["close"] for j in range(idx - 1, max(idx - 6, -1), -1)]
    need = 5 - len(closes)
    if need > 0 and prior_daily_closes:
        first_bar_date = bars[max(idx - len(closes) - 1, 0)]["date"]
        prior_dates = sorted(d for d in prior_daily_closes if d < first_bar_date)
        for d in prior_dates[-need:]:
            closes.append(prior_daily_closes[d])
    if len(closes) < 3 or closes[0] >= closes[-1]:
        return False
    # Magnitude floor: closes[0] is the most-recent bar, closes[-1] the oldest;
    # a real downtrend needs the recent close at least 1.5% below the older one,
    # not just any tick-level drift.
    return (closes[0] - closes[-1]) / closes[-1] <= -0.015


def pat_hammer(bars, i, prior):
    if i < 1: return False
    b = bars[i]
    if not _is_bull(b): return False
    if _body(b) < 0.001 * b["close"]: return False   # kill doji degeneracy (body=0 trivially passes _lower>=2*body)
    if _body(b) > 0.3 * _range(b): return False
    if _lower(b) < 2 * _body(b):  return False
    if _upper(b) > 0.5 * _body(b): return False
    return _downtrend_before(bars, i, prior)


def pat_engulfing(bars, i, prior):
    if i < 1: return False
    prev, curr = bars[i - 1], bars[i]
    if not (_is_bear(prev) and _is_bull(curr)): return False
    if not (curr["open"] <= prev["close"] and curr["close"] >= prev["open"]):
        return False
    return _downtrend_before(bars, i, prior)


def pat_morning_star(bars, i, prior):
    if i < 2: return False
    d1, d2, d3 = bars[i - 2], bars[i - 1], bars[i]
    if not _is_bear(d1) or _body(d1) < 0.4 * _range(d1): return False
    if _body(d2) > 0.3 * _range(d2): return False
    if not _is_bull(d3): return False
    mid_d1 = (d1["open"] + d1["close"]) / 2
    if d3["close"] < mid_d1: return False
    return _downtrend_before(bars, i, prior)


def pat_piercing(bars, i, prior):
    if i < 1: return False
    prev, curr = bars[i - 1], bars[i]
    if not (_is_bear(prev) and _is_bull(curr)): return False
    if _body(prev) < 0.4 * _range(prev): return False
    if curr["open"] >= prev["close"]: return False
    mid_prev = (prev["open"] + prev["close"]) / 2
    if curr["close"] <= mid_prev:     return False
    if curr["close"] >= prev["open"]: return False  # else it's engulfing
    return _downtrend_before(bars, i, prior)


def pat_three_white(bars, i, prior):
    if i < 2: return False
    d1, d2, d3 = bars[i - 2], bars[i - 1], bars[i]
    for d in (d1, d2, d3):
        if not _is_bull(d):              return False
        if _body(d) < 0.6 * _range(d):   return False
        if _upper(d) > 0.3 * _body(d):   return False
    return (d2["close"] > d1["close"] and d3["close"] > d2["close"]
        and d2["open"]  > d1["open"]  and d3["open"]  > d2["open"])


def pat_tweezer_bottom(bars, i, prior):
    if i < 1: return False
    prev, curr = bars[i - 1], bars[i]
    tol = max(prev["low"], curr["low"]) * 0.003
    if abs(prev["low"] - curr["low"]) > tol: return False
    if not _is_bull(curr): return False
    return _downtrend_before(bars, i, prior)


PATTERNS = [
    ("Hammer",               pat_hammer),
    ("Bullish Engulfing",    pat_engulfing),
    ("Morning Star",         pat_morning_star),
    ("Piercing Line",        pat_piercing),
    ("Three White Soldiers", pat_three_white),
    ("Tweezer Bottom",       pat_tweezer_bottom),
]


def detect_patterns_on_yesterday(bars, daily_closes):
    """Run all 6 patterns on the LATEST COMPLETED bar.

    "Latest completed" = bars[-1] when bars[-1].date < today; otherwise bars[-2]
    (today's bar is still forming during market hours). Returns a list of
    pattern names that triggered. Empty list if no pattern fires or if there
    aren't enough bars."""
    if not bars:
        return []
    today = datetime.now().date()
    # Pick the last completed bar — drop today's in-progress bar if present
    if bars[-1]["date"] >= today:
        idx = len(bars) - 2
    else:
        idx = len(bars) - 1
    if idx < 1:
        return []
    hits = []
    for name, fn in PATTERNS:
        try:
            if fn(bars, idx, daily_closes):
                hits.append(name)
        except Exception:
            pass
    return hits


# ── Universe + filters ───────────────────────────────────────────────────────

def is_friday_today():
    return ec.is_friday()


def past_late_entry_cutoff():
    return ec.past_cutoff(LATE_ENTRY_CUTOFF_MIN)


# ── Main run ─────────────────────────────────────────────────────────────────

def main():
    rotate_log()
    log = open(LOG_PATH, "a", encoding="utf-8")

    def out(msg=""):
        log.write(msg + "\n")
        log.flush()

    state = load_state()
    now_local_ts = datetime.now().replace(microsecond=0).isoformat()
    today = ec.today_local()   # local Stockholm trade date (feed is market-local)

    out("=" * 72)
    out(f" Live patterns run @ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} local")
    out("=" * 72)

    if not L.is_market_open():
        out(" Market closed (Stockholm OMX 09:00-17:30 Mon-Fri). Skipping.")
        log.close()
        return

    # ── 1. Universe + live quotes ────────────────────────────────────────────
    primary, _ = L.build_universe()
    live = L.fetch_live_parallel(primary)

    # Ensure open-position tickers have live quotes too (for exit checks)
    for tk in state["positions"]:
        if tk in live:
            continue
        insref = state["positions"][tk].get("insref")
        if insref:
            q = L.get_live(insref)
            if q.get("ok"):
                live[tk] = q
            else:
                # Tag as 'stale' so apply_exit_checks won't trigger TP/SL
                # against the entry-price placeholder.
                live[tk] = {"last": state["positions"][tk]["entry_price"],
                            "ok": True, "source": "stale"}

    # ── 2. Apply exit checks (reuses live_signals' limit-fill logic).
    # Honour user-queued manual sells from live_patterns_force_sell.json.
    force_sells = L.load_force_sells(FORCE_SELL_PATH)
    closed_now  = L.apply_exit_checks(state, today, now_local_ts, live, force_sells)
    L.consume_force_sells(
        {t["ticker"] for t in closed_now if t["reason"] == "MANUAL"},
        path=FORCE_SELL_PATH,
    )
    if closed_now:
        out(f"\n--- CLOSED THIS RUN ({len(closed_now)}) ---")
        for t in closed_now:
            out(f"  [PAT]     {t['ticker']:<22} {t['reason']:<7} @ {t['exit_price']:.2f}   "
                f"P&L {t['pnl_sek']:+,.0f} SEK ({t['ret_pct']*100:+.2f}%)   "
                f"opened {t['entry_date']}  pattern={(t.get('signal_info') or {}).get('pattern','?')}")

    # ── 3. Skip new-entry stage if we're past the late-entry cutoff or Friday ──
    if is_friday_today():
        out(" No entries on Friday (weekend-risk rule).")
        _report_open(state, live, out)
        _persist_ui_snapshot(state, live,
                             entry_gate="no entries on Friday (weekend-risk rule)",
                             ts=now_local_ts)
        save_state(state)
        log.close()
        return
    if past_late_entry_cutoff():
        out(f" Past 16:30 cutoff ({datetime.now().strftime('%H:%M')}) — no new entries.")
        _report_open(state, live, out)
        _persist_ui_snapshot(
            state, live,
            entry_gate=f"past the 16:30 late-entry cutoff ({datetime.now().strftime('%H:%M')})",
            ts=now_local_ts)
        save_state(state)
        log.close()
        return

    # ── 4. Build cooldown lookup (same rules as live_signals) ────────────────
    recent_exits = ec.build_recent_exits(state)

    held_stems = {L.company_stem(tk) for tk in state["positions"]}
    foreign_stems = (ec.foreign_held_stems(ec.sibling_wallets("patterns"), L.company_stem)
                     if CROSS_WALLET_DEDUPE else {})

    # ── 5. Pattern scan: for each tradable, detect on latest completed bar ───
    pattern_fires = []
    for t in primary:
        name = t["name"]
        if name in state["positions"]:
            continue
        # Bugfix 2026-05-25: build_universe() stores `folder` as the bare
        # basename (e.g. "104_HexagonB"), not a full path. Prepend BASE so
        # the *_7d.csv glob actually resolves. Before this fix the engine
        # silently returned 0 bars for every stock and produced 0 entries.
        bars = aggregate_to_daily_ohlc(load_intraday_minute_closes(os.path.join(L.BASE, t["folder"])))
        if len(bars) < 2:
            continue
        # Daily-history closes for the wider trend context
        daily_closes = {}
        try:
            for ts, p in L.load_history_closes(t["csv"]):
                daily_closes[datetime.fromtimestamp(ts / 1000, tz=timezone.utc).date()] = p
        except OSError:
            pass

        # Crash guard
        all_closes = sorted(daily_closes.keys())
        if len(all_closes) >= 21:
            ret20 = daily_closes[all_closes[-1]] / daily_closes[all_closes[-21]] - 1.0
            if ret20 <= CRASH_GUARD:
                continue

        hits = detect_patterns_on_yesterday(bars, daily_closes)
        if hits:
            # "patterns" drives entries and the confluence count; "all_detected"
            # keeps the full picture for the log and for /live-patterns.
            pattern_fires.append({
                "t": t,
                "patterns":     [h for h in hits if h not in ENTRY_DISABLED_PATTERNS],
                "all_detected": hits,
            })

    # ── 6. Enter positions for unique fires (respecting cooldown / stem dedup) ──
    new_entries = []
    new_skipped = []
    # Block entries near dividend ex-dates / earnings reports (added 2026-05-25).
    events_cache = calendar_cache.load_cache()
    for fire in pattern_fires:
        t = fire["t"]
        name = t["name"]
        # Confluence gate (2026-06-13) — solo-pattern fires were net-negative
        # in the first 30 trades. CONFLUENCE_REQUIRED at module head can
        # disable this without other changes.
        if not fire["patterns"]:
            disabled = ", ".join(fire["all_detected"])
            new_skipped.append({"ticker": name,
                                "reason": f"only entry-disabled pattern(s): {disabled}"})
            continue
        if CONFLUENCE_REQUIRED and len(fire["patterns"]) < CONFLUENCE_MIN_COUNT:
            new_skipped.append({"ticker": name,
                                "reason": f"solo {fire['patterns'][0]} (confluence requires {CONFLUENCE_MIN_COUNT}+ patterns)"})
            continue
        stem = L.company_stem(name)
        if stem in held_stems:
            new_skipped.append({"ticker": name, "reason": f"duplicate stem '{stem}'"})
            continue
        if stem in foreign_stems:
            new_skipped.append({"ticker": name,
                                "reason": f"held in another wallet ({foreign_stems[stem]})"})
            continue
        blocked, why = calendar_cache.has_nearby_event(t["insref"], today, cache=events_cache)
        if blocked:
            new_skipped.append({"ticker": name, "reason": f"event window: {why}"})
            continue
        cd = ec.cooldown_status(recent_exits, name, today, COOLDOWN_DAYS, L.trading_days_between)
        if cd:
            if cd["kind"] == "SL":
                new_skipped.append({"ticker": name, "reason": f"SL cooldown {cd['tds']}/{COOLDOWN_DAYS}d since {cd['last_date']}"})
            else:
                new_skipped.append({"ticker": name, "reason": f"same-day re-entry blocked (just exited {cd['last_reason']})"})
            continue
        lq = live.get(name)
        if not lq or not lq.get("ok") or lq.get("last") in (None, 0):
            new_skipped.append({"ticker": name, "reason": "no live quote"})
            continue
        if state["cash"] < POS_MIN:
            new_skipped.append({"ticker": name, "reason": f"low cash {state['cash']:.0f}"})
            continue
        entry_price = lq.get("ask") or lq["last"]
        # Liquidity gate: this engine's 2026-08-24 Electrolux A fill was 53% of
        # that name's entire day volume. Caps the position rather than only
        # rejecting it, so a mid-liquidity ticker can still take a smaller size.
        liq = ec.liquidity_verdict(t["insref"], entry_price, lq)
        cap, liq_skip = ec.apply_liquidity_cap(liq, CAP_SIZE, POS_MIN)
        if liq_skip:
            new_skipped.append({"ticker": name, "reason": liq_skip})
            continue
        shares, cost = ec.position_size(state["cash"], entry_price, TARGET_SIZE, cap, POS_MIN)
        if shares <= 0:
            new_skipped.append({"ticker": name, "reason": f"size <{POS_MIN/1000:.0f}k after rounding"})
            continue
        tp_price = round(entry_price * (1 + TP_PCT), 4)
        sl_price = round(entry_price * (1 + SL_PCT), 4)
        exit_by  = L.add_trading_days(today, MAX_HOLD_DAYS)
        primary_pattern = fire["patterns"][0]    # take first if multiple fired
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
            "signal_info": {
                "entry_tag":     "pattern",
                "conviction":    "normal",
                "pattern":       primary_pattern,
                "all_patterns":  fire["patterns"],
            },
            **V.stamp("patterns"),
        }
        held_stems.add(stem)
        new_entries.append({
            "ticker": name, "pattern": primary_pattern, "all_patterns": fire["patterns"],
            "entry": entry_price, "shares": shares, "cost": cost,
            "tp": tp_price, "sl": sl_price, "exit_by": exit_by.isoformat(),
        })

    # ── 7. Reporting ──────────────────────────────────────────────────────────
    open_value = sum(p["shares"] * (live.get(tk, {}).get("last") or p["entry_price"])
                     for tk, p in state["positions"].items())
    equity = state["cash"] + open_value
    realized = sum(t["pnl_sek"] for t in state["closed_trades"])
    out(f"\nWallet:    cash {state['cash']:>10,.0f}   open {open_value:>10,.0f}   "
        f"equity {equity:>10,.0f}  SEK")
    out(f"P&L:       realized {realized:+,.0f} SEK   "
        f"open positions {len(state['positions'])}   "
        f"closed trades {len(state['closed_trades'])}")

    if new_entries:
        out(f"\n--- NEW PATTERN ENTRIES ({len(new_entries)}) ---")
        for e in new_entries:
            out(f"  BUY [PAT]   {e['ticker']:<22} {e['shares']:>5} sh @ {e['entry']:.2f}  "
                f"= {e['cost']:>9,.0f} SEK   {e['pattern']}")
            out(f"      target {e['tp']:.2f} (+{TP_PCT*100:.2f}%)  "
                f"stop {e['sl']:.2f} ({SL_PCT*100:+.2f}%)  exit by {e['exit_by']}")
    else:
        out("\n--- NO NEW PATTERN ENTRIES ---")

    if new_skipped:
        out(f"\n--- PATTERN HITS SKIPPED ({len(new_skipped)}) ---")
        for s in new_skipped[:10]:
            out(f"  skipped {s['ticker']:<22} {s['reason']}")
        if len(new_skipped) > 10:
            out(f"  ... +{len(new_skipped) - 10} more skipped")

    _report_open(state, live, out)

    _persist_ui_snapshot(state, live, primary, skipped=new_skipped, ts=now_local_ts)
    # live_cross consumes this. "patterns" is the entry-eligible set, so a
    # disabled pattern can never reach Cross as a tradable fire either.
    state["recent_pattern_fires"] = [
        {"ticker": f["t"]["name"], "patterns": f["patterns"],
         "all_detected": f["all_detected"], "ts": now_local_ts}
        for f in pattern_fires[-50:]
    ]

    save_state(state)
    log.close()


def _report_open(state, live, out):
    ec.report_open(state, live, out, "  [PAT]     ")


def _persist_ui_snapshot(state, live, primary=(), skipped=(), entry_gate=None, ts=None):
    """Refresh the UI-facing fields (`ticker_paths`, `last_quotes`) from this
    run's live quotes.

    Must run on *every* exit path. The Friday and post-16:30 branches return
    before the entry stage, and they used to call save_state() without touching
    this — so last_run kept advancing while /live-patterns redrew whatever
    prices the last full run had left behind. The Open positions table looked
    live and was frozen, and tickers closed since that run lingered as orphan
    keys in last_quotes.

    Quotes are keyed on the open positions only. Prior known-good quotes are
    preserved when this run's fetch failed or fell back to a synthetic price
    (build_quote_snapshot re-tags those 'stale') — same rationale as
    live_signals: never display an entry-price placeholder as 'Current'.

    `primary` is the scanned universe, empty on the early-return paths — those
    still refresh every open position's path from its insref below.
    """
    # ticker -> stock-page rel_path for UI click-through (mirrors
    # live_signals.py). rel_path matches what /view?path=... expects.
    ticker_paths = state.get("ticker_paths", {}) or {}
    for t in primary:
        if t.get("folder"):
            ticker_paths[t["name"]] = f"Equity/SEK/{t['folder']}"
    for tk, pos in state["positions"].items():
        if tk in ticker_paths:
            continue
        insref = pos.get("insref")
        if insref:
            folder, _ = L._find_history_csv(insref)
            if folder:
                ticker_paths[tk] = f"Equity/SEK/{folder}"
    state["ticker_paths"] = ticker_paths

    prior_quotes = state.get("last_quotes", {}) or {}
    state["last_quotes"] = ec.build_quote_snapshot(
        state["positions"].keys(), live, prior_quotes, "live-patterns")

    # This run's gate decisions, for the "Blocked this run" panel. Written here
    # rather than at the entry stage precisely because the Friday and post-16:30
    # branches never reach that stage: leaving the field alone would republish
    # the previous run's rejections under a fresh `last_run`, which is the
    # stale-data-behind-a-fresh-timestamp trap that hid frozen prices on this
    # same page for weeks. `entry_gate` says *why* nothing was evaluated, which
    # is strictly more informative than an empty list.
    state["last_skipped"]     = [{**s, "ts": ts} for s in (skipped or ())]
    state["last_entry_gate"]  = entry_gate


if __name__ == "__main__":
    main()
