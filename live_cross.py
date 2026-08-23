"""
Live Cross — confluence paper-trading engine, launched 2026-06-13.

Combines the Live Signals (validated +2/-1/7d mean-reversion) universe with
Live Patterns' recent candlestick fires. Surfaces tickers that BOTH engines
have visible at the same time, but does not auto-enter — the user approves
each opportunity in the /live-cross UI before the engine opens a position.

Design choices (per 2026-06-13 design discussion)
  · No new pattern detection. We read `recent_pattern_fires` from the Patterns
    engine's state file as the patterns signal — single source of truth.
  · Broader radar overlap on the Signals side: a ticker counts as "in Signals'
    radar" if it is in `last_universe ∪ last_watchlist`. This is the broadest
    set Signals knows how to evaluate.
  · User decides every trigger. No auto-entry — engine only opens a position
    when a candidate has been explicitly added to `live_cross_approved.json`.

Strategy params (held identical to Signals + Patterns)
  Entry:   user-approved candidate (no auto-fire)
  Filters: not Friday · not after 16:30 · ret_20d > -15% · calendar event window
           · stem-dedup vs current positions · SL cooldown 1 trading day (same-day block only)
  Exits:   TP +2.0%, SL -2.0%, max 7-day hold (limit-fill capped via
           apply_exit_checks reused from live_signals)
  Sizing:  target 70k, cap 85k, floor 35k against a 500k SEK wallet
  Wallet:  separate 500k SEK at /live-cross — apples-to-apples with the
           other two engines

Run lifecycle
  Called from live_signals.__main__ after live_patterns.main() finishes,
  inside the same 3-min LaunchAgent cadence. We use the same load_state /
  save_state / apply_exit_checks helpers as live_signals so cooldown,
  limit-fill capping, conviction sizing all behave identically.
"""

import csv
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

import env_loader                                    # noqa: E402
env_loader.load_env()
import engine_common as ec                           # noqa: E402
import live_signals as L                             # reuse helpers          # noqa: E402
import version as V                                  # noqa: E402
import calendar_cache                                # noqa: E402

ROOT       = os.path.dirname(os.path.abspath(__file__))
STATE_DIR  = os.path.join(ROOT, "state")
LOGS_DIR   = os.path.join(ROOT, "logs")

STATE_PATH       = os.path.join(STATE_DIR, "live_cross_portfolio.json")
LOG_PATH         = os.path.join(LOGS_DIR,  "live_cross.log")
APPROVED_PATH    = os.path.join(STATE_DIR, "live_cross_approved.json")
FORCE_SELL_PATH  = os.path.join(STATE_DIR, "live_cross_force_sell.json")

# Upstream engines we read from
SIGNALS_STATE_PATH  = os.path.join(STATE_DIR, "live_portfolio.json")
PATTERNS_STATE_PATH = os.path.join(STATE_DIR, "live_patterns_portfolio.json")

WALLET_START  = 500_000.0
POS_MIN       =  35_000.0
TARGET_SIZE   =  70_000.0
CAP_SIZE      =  85_000.0

TP_PCT        =  0.020
SL_PCT        = -0.020
MAX_HOLD_DAYS = 7
COOLDOWN_DAYS = 1   # SL-exit re-entry block (reduced from 7 → 1 on 2026-06-13: same-day block only, next trading day allowed)
CRASH_GUARD   = -0.15
LATE_ENTRY_CUTOFF_MIN = 16 * 60 + 30   # 16:30 local — no new entries after

# How recent a pattern fire needs to be for a ticker to count as "Patterns
# is active on this name". Patterns engine writes pattern fires each run
# with the run's `ts`; we keep candidates fresh for one full trading day.
PATTERN_FIRE_FRESH_HOURS = 30

# Entries paused 2026-08-18. The premise of this wallet is that Signals ∩ Patterns
# yields *higher*-quality entries; the first 20 scored trades say it inverts them.
# Tweezer Bottom runs 69.2% WR (n=13) in the Patterns wallet but 25.0% (n=8) here,
# and Three White Soldiers 100% (n=1) there against 25.0% (n=4) here. Cross also
# went from "zero strict confluences in 3 weeks" at the 2026-06-13 audit to 24
# closed trades, so the gate it is now testing is not the one it was launched with.
# Exits, reporting and candidate computation all keep running — this only stops
# new capital going in. Flip to False to resume.
ENTRIES_PAUSED = True
ENTRIES_PAUSED_SINCE  = "2026-08-18"
ENTRIES_PAUSED_REASON = ("Cross inverts the edge of the Patterns wallet it draws from — "
                         "Tweezer Bottom runs 69% WR (n=13) in Patterns against 25% (n=8) "
                         "here. Exits, candidates and reporting still run.")

LOG_RETAIN_HOURS     = 24
LOG_ROTATE_MIN_BYTES = 200_000


# ── State persistence ────────────────────────────────────────────────────────

def load_state():
    return ec.load_state(STATE_PATH, {
        "cash":          WALLET_START,
        "positions":     {},
        "closed_trades": [],
        "last_run":      None,
    })


def save_state(state):
    ec.save_state(STATE_PATH, state)


def load_approved():
    data = ec.read_json(APPROVED_PATH, [])
    return set(data) if isinstance(data, list) else set()


def consume_approvals(consumed):
    if not consumed:
        return
    remaining = load_approved() - set(consumed)
    ec.write_json_atomic(APPROVED_PATH, sorted(remaining), ensure_ascii=False)


def rotate_log():
    """Trim live_cross.log to the last 24h — see engine_common.rotate_log."""
    ec.rotate_log(LOG_PATH, " Live cross run @ ", LOG_RETAIN_HOURS, LOG_ROTATE_MIN_BYTES)


# ── Upstream-state loaders ───────────────────────────────────────────────────

def _read_json(path, default):
    return ec.read_json(path, default)


def _parse_ts(s):
    """Tolerate the mix of formats used across engines (local naive vs UTC)."""
    if not s or not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def fresh_pattern_fires(patterns_state, now_local):
    """Return {ticker: {'patterns': [...], 'ts': iso}} for pattern fires recent
    enough to still count as 'Patterns is active on this name'. Stale fires
    age out after PATTERN_FIRE_FRESH_HOURS so the candidate list doesn't grow
    forever between Patterns engine runs."""
    fires = patterns_state.get("recent_pattern_fires") or []
    cutoff = now_local - timedelta(hours=PATTERN_FIRE_FRESH_HOURS)
    fresh = {}
    for f in fires:
        ts = _parse_ts(f.get("ts"))
        # `ts` is local naive from live_patterns; treat None as fresh so an
        # engine that hasn't yet timestamped its fires doesn't go silent.
        # If a tz-aware ts ever arrives, drop the tzinfo so it compares against
        # the naive-local `cutoff` without raising.
        if ts is not None and ts.tzinfo is not None:
            ts = ts.replace(tzinfo=None)
        if ts is not None and ts < cutoff:
            continue
        tk = f.get("ticker")
        if not tk:
            continue
        fresh[tk] = {"patterns": f.get("patterns") or [], "ts": f.get("ts")}
    return fresh


def signals_radar(signals_state):
    """Tickers Signals' last run treated as in-scope: large-cap universe plus
    mid/small-cap watchlist. Broadest set Signals knows how to evaluate.
    Returns set of ticker names."""
    uni = set(signals_state.get("last_universe") or [])
    wl  = set(signals_state.get("last_watchlist") or [])
    return uni | wl


# ── Universe + filters ───────────────────────────────────────────────────────

def is_friday_today():
    return ec.is_friday()


def past_late_entry_cutoff():
    return ec.past_cutoff(LATE_ENTRY_CUTOFF_MIN)


# ── Candidate computation ────────────────────────────────────────────────────

def compute_candidates(signals_state, patterns_state, state, today, now_local):
    """Build the list of cross-radar candidates.

    A ticker is a candidate when:
      · It's in Signals' last_universe OR last_watchlist (broader radar)
      · AND Patterns has a fresh recent_pattern_fires entry on it
        (within PATTERN_FIRE_FRESH_HOURS hours)
      · AND we are not already long it on the Cross engine

    For each candidate we record any blocking reasons (calendar window, SL
    cooldown, Friday, late-cutoff, low cash, stem dup) so the UI can show
    the user why approving would be a no-op. The reasons don't prevent the
    candidate from appearing — they just disable the Approve button.
    """
    radar = signals_radar(signals_state)
    fires = fresh_pattern_fires(patterns_state, now_local)

    # Build cooldown/recent-exit lookup for Cross's own trades
    recent_exits = ec.build_recent_exits(state)

    held_stems = {L.company_stem(tk) for tk in state["positions"]}

    # Build insref lookup from the upstream engines' ticker_paths and stock_names.csv
    insref_map = _insref_map(signals_state, patterns_state)

    # Tier lookup (large cap vs mid/small) from Signals' separation
    sig_uni = set(signals_state.get("last_universe") or [])
    sig_wl  = set(signals_state.get("last_watchlist") or [])

    # Tradable-folder paths for UI click-through
    ticker_paths = {}
    for src in (signals_state.get("ticker_paths") or {}, patterns_state.get("ticker_paths") or {}):
        for tk, rp in src.items():
            ticker_paths.setdefault(tk, rp)

    events_cache = calendar_cache.load_cache()

    candidates = []
    for tk in sorted(radar & set(fires)):
        if tk in state["positions"]:
            continue   # already long it on Cross
        insref = insref_map.get(tk)
        skip = []
        if ENTRIES_PAUSED:
            skip.append(f"entries paused since {ENTRIES_PAUSED_SINCE}")
        # Calendar window block
        if insref:
            blocked, why = calendar_cache.has_nearby_event(insref, today, cache=events_cache)
            if blocked:
                skip.append(f"event window: {why}")
        # Cooldown
        cd = ec.cooldown_status(recent_exits, tk, today, COOLDOWN_DAYS, L.trading_days_between)
        if cd:
            if cd["kind"] == "SL":
                skip.append(f"SL cooldown {cd['tds']}/{COOLDOWN_DAYS}d")
            else:
                skip.append(f"same-day re-entry blocked ({cd['last_reason']})")
        # Stem dedup
        stem = L.company_stem(tk)
        if stem in held_stems:
            skip.append(f"duplicate stem '{stem}'")
        # Friday / late cutoff
        if is_friday_today():
            skip.append("no Friday entries")
        if past_late_entry_cutoff():
            skip.append("past 16:30 cutoff")
        # Cash floor
        if state["cash"] < POS_MIN:
            skip.append(f"low cash {state['cash']:.0f}")

        candidates.append({
            "ticker":          tk,
            "insref":          insref,
            "patterns":        fires[tk]["patterns"],
            "pattern_fire_ts": fires[tk]["ts"],
            "tier":            "large cap" if tk in sig_uni else ("mid/small" if tk in sig_wl else "?"),
            "in_signals_radar": True,
            "ticker_path":     ticker_paths.get(tk),
            "skip_reasons":    skip,
            "blocked":         bool(skip),
        })
    return candidates


def _insref_map(signals_state, patterns_state):
    """ticker -> insref, drawn from any source we have."""
    out = {}
    # Positions on either engine carry insref directly
    for src in (signals_state, patterns_state):
        for tk, pos in (src.get("positions") or {}).items():
            ir = pos.get("insref")
            if ir:
                out[tk] = int(ir)
    # Fall back to stock_names.csv (insref is the first column)
    try:
        names_csv = os.path.join(ROOT, "stock_names.csv")
        if os.path.exists(names_csv):
            with open(names_csv, newline="", encoding="utf-8") as f:
                for row in csv.reader(f):
                    if len(row) >= 2:
                        try:
                            out.setdefault(row[1], int(row[0]))
                        except ValueError:
                            pass
    except OSError:
        pass
    return out


# ── Main run ─────────────────────────────────────────────────────────────────

def main():
    rotate_log()
    log = open(LOG_PATH, "a", encoding="utf-8")

    def out(msg=""):
        log.write(msg + "\n")
        log.flush()

    state = load_state()
    now_local      = datetime.now()
    now_local_ts   = now_local.replace(microsecond=0).isoformat()
    today          = ec.today_local()   # local Stockholm trade date (feed is market-local)

    out("=" * 72)
    out(f" Live cross run @ {now_local.strftime('%Y-%m-%d %H:%M:%S')} local")
    out("=" * 72)

    if not L.is_market_open():
        out(" Market closed (Stockholm OMX 09:00-17:30 Mon-Fri). Skipping.")
        log.close()
        return

    # ── 1. Load upstream engine state ────────────────────────────────────────
    signals_state  = _read_json(SIGNALS_STATE_PATH,  {})
    patterns_state = _read_json(PATTERNS_STATE_PATH, {})

    # ── 2. Compute candidate set + persist for UI ───────────────────────────
    candidates = compute_candidates(signals_state, patterns_state, state, today, now_local)
    state["pending_candidates"] = candidates
    state["entries_paused"]     = ENTRIES_PAUSED
    state["paused_since"]       = ENTRIES_PAUSED_SINCE  if ENTRIES_PAUSED else None
    state["paused_reason"]      = ENTRIES_PAUSED_REASON if ENTRIES_PAUSED else None
    out(f" Cross-radar candidates: {len(candidates)}  "
        f"(signals_radar={len(signals_radar(signals_state))}, "
        f"fresh_fires={len(fresh_pattern_fires(patterns_state, now_local))})")
    if candidates:
        for c in candidates[:10]:
            tag = " · ".join(c["skip_reasons"]) if c["skip_reasons"] else "open"
            pats = ", ".join(c["patterns"])
            out(f"   - {c['ticker']:<22} [{c['tier']:<10}] {pats:<32} {tag}")
        if len(candidates) > 10:
            out(f"   …+{len(candidates) - 10} more")

    # ── 3. Live quotes for the union of (open positions + candidates) ──────
    live = {}
    # Positions need quotes for exits
    pos_tickers = [{"name": tk, "insref": pos.get("insref")}
                   for tk, pos in state["positions"].items() if pos.get("insref")]
    cand_tickers = [{"name": c["ticker"], "insref": c["insref"]}
                    for c in candidates if c["insref"]]
    fetch_targets = {t["name"]: t for t in pos_tickers + cand_tickers}.values()
    if fetch_targets:
        live = L.fetch_live_parallel(list(fetch_targets))
    # Ensure open positions have at least a stale placeholder for apply_exit_checks
    for tk, pos in state["positions"].items():
        if tk not in live or not live[tk].get("ok"):
            live[tk] = {"last": pos["entry_price"], "ok": True, "source": "stale"}

    # ── 4. Exit checks (limit-fill cap, MANUAL/TP/SL/TIMEOUT) ───────────────
    force_sells = L.load_force_sells(FORCE_SELL_PATH)
    closed_now  = L.apply_exit_checks(state, today, now_local_ts, live, force_sells)
    L.consume_force_sells(
        {t["ticker"] for t in closed_now if t["reason"] == "MANUAL"},
        path=FORCE_SELL_PATH,
    )
    if closed_now:
        out(f"\n--- CLOSED THIS RUN ({len(closed_now)}) ---")
        for t in closed_now:
            out(f"  [X]  {t['ticker']:<22} {t['reason']:<7} @ {t['exit_price']:.2f}   "
                f"P&L {t['pnl_sek']:+,.0f} SEK ({t['ret_pct']*100:+.2f}%)   "
                f"opened {t['entry_date']}")

    # ── 5. New entries — ONLY for approved candidates ───────────────────────
    if ENTRIES_PAUSED:
        out(" ENTRIES PAUSED (ENTRIES_PAUSED=True) — exits and reporting still run.")
    if is_friday_today():
        out(" No entries on Friday (weekend-risk rule).")
    if past_late_entry_cutoff():
        out(f" Past 16:30 cutoff ({now_local.strftime('%H:%M')}) — no new entries.")

    approved = load_approved()
    if approved:
        out(f"\n Approved for entry: {sorted(approved)}")

    new_entries = []
    new_skipped = []
    consumed_approvals = set()

    if approved and not ENTRIES_PAUSED and not is_friday_today() and not past_late_entry_cutoff():
        cand_by_ticker = {c["ticker"]: c for c in candidates}
        for tk in sorted(approved):
            cand = cand_by_ticker.get(tk)
            if not cand:
                new_skipped.append({"ticker": tk, "reason": "no current cross-radar candidate"})
                # Don't consume — keep the approval until a candidate appears
                continue
            if cand["blocked"]:
                new_skipped.append({"ticker": tk, "reason": " · ".join(cand["skip_reasons"])})
                # Don't consume — let the user retry once the block clears
                continue
            lq = live.get(tk)
            if not lq or not lq.get("ok") or lq.get("last") in (None, 0):
                new_skipped.append({"ticker": tk, "reason": "no live quote"})
                continue
            if state["cash"] < POS_MIN:
                new_skipped.append({"ticker": tk, "reason": f"low cash {state['cash']:.0f}"})
                continue
            entry_price = lq.get("ask") or lq["last"]
            shares, cost = ec.position_size(state["cash"], entry_price, TARGET_SIZE, CAP_SIZE, POS_MIN)
            if shares <= 0:
                new_skipped.append({"ticker": tk, "reason": f"size <{POS_MIN/1000:.0f}k after rounding"})
                continue
            tp_price = round(entry_price * (1 + TP_PCT), 4)
            sl_price = round(entry_price * (1 + SL_PCT), 4)
            exit_by  = L.add_trading_days(today, MAX_HOLD_DAYS)
            primary_pattern = cand["patterns"][0] if cand["patterns"] else "?"
            state["cash"] -= cost
            state["positions"][tk] = {
                "insref":           cand["insref"],
                "entry_price":      entry_price,
                "shares":           shares,
                "entry_date":       today.isoformat(),
                "entry_ts":         now_local_ts,
                "tp":               tp_price,
                "sl":               sl_price,
                "target_exit_date": exit_by.isoformat(),
                "cost_sek":         cost,
                "signal_info": {
                    "entry_tag":      "cross",
                    "conviction":     "normal",
                    "pattern":        primary_pattern,
                    "all_patterns":   cand["patterns"],
                    "signals_tier":   cand["tier"],
                    "approved_at":    now_local_ts,
                },
                **V.stamp("cross"),
            }
            new_entries.append({
                "ticker": tk, "pattern": primary_pattern, "all_patterns": cand["patterns"],
                "tier": cand["tier"], "entry": entry_price, "shares": shares, "cost": cost,
                "tp": tp_price, "sl": sl_price, "exit_by": exit_by.isoformat(),
            })
            consumed_approvals.add(tk)

    if consumed_approvals:
        consume_approvals(consumed_approvals)

    # ── 6. Reporting ────────────────────────────────────────────────────────
    open_value = sum(p["shares"] * (live.get(tk, {}).get("last") or p["entry_price"])
                     for tk, p in state["positions"].items())
    equity   = state["cash"] + open_value
    realized = sum(t["pnl_sek"] for t in state["closed_trades"])
    out(f"\nWallet:    cash {state['cash']:>10,.0f}   open {open_value:>10,.0f}   "
        f"equity {equity:>10,.0f}  SEK")
    out(f"P&L:       realized {realized:+,.0f} SEK   "
        f"open positions {len(state['positions'])}   "
        f"closed trades {len(state['closed_trades'])}")

    if new_entries:
        out(f"\n--- NEW CROSS ENTRIES ({len(new_entries)}) ---")
        for e in new_entries:
            out(f"  BUY [X]   {e['ticker']:<22} {e['shares']:>5} sh @ {e['entry']:.2f}  "
                f"= {e['cost']:>9,.0f} SEK   {e['pattern']} ({e['tier']})")
            out(f"      target {e['tp']:.2f} (+{TP_PCT*100:.2f}%)  "
                f"stop {e['sl']:.2f} ({SL_PCT*100:+.2f}%)  exit by {e['exit_by']}")
    elif approved:
        out("\n--- NO NEW CROSS ENTRIES ("
            + ("entries paused" if ENTRIES_PAUSED else "approvals queued or blocked") + ") ---")

    if new_skipped:
        out(f"\n--- APPROVALS SKIPPED ({len(new_skipped)}) ---")
        for s in new_skipped[:10]:
            out(f"  skipped {s['ticker']:<22} {s['reason']}")

    _report_open(state, live, out)

    # ── 7. UI snapshot: persist last_quotes + ticker_paths ──────────────────
    # ticker_paths: union of upstream engines + candidate paths (so UI links work)
    ticker_paths = state.get("ticker_paths", {}) or {}
    for src in (signals_state.get("ticker_paths") or {}, patterns_state.get("ticker_paths") or {}):
        for tk, rp in src.items():
            ticker_paths.setdefault(tk, rp)
    for c in candidates:
        if c.get("ticker_path"):
            ticker_paths.setdefault(c["ticker"], c["ticker_path"])
    state["ticker_paths"] = ticker_paths

    prior_quotes = state.get("last_quotes", {}) or {}
    quote_tickers = set(state["positions"].keys()) | {c["ticker"] for c in candidates}
    state["last_quotes"] = ec.build_quote_snapshot(quote_tickers, live, prior_quotes, "live-cross")

    save_state(state)
    log.close()


def _report_open(state, live, out):
    ec.report_open(state, live, out, "  [X]  ")


if __name__ == "__main__":
    main()
