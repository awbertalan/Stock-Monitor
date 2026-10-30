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
  Exits:     TP +2.0%, SL -1.0%, time-out at 7 trading days  (limit-fill capped)
             — back to the backtested pair (40.9% WR, +72 SEK/trade, profitable
             in 10 of 11 years). Between 2026-05-25 and 2026-08-24 the stop ran
             at -2.0%, widened to survive opening volatility but never
             re-backtested; at 1:1 that needs 50% WR and the engine delivered
             43.8% (7W/9L over 16 scored, -388 SEK/trade). At 2:1 the
             break-even is 33.3%. Expect more stop-outs than at -2.0% — that is
             the trade the backtest says is still worth making.
  Cooldown:  SL/TP/TIMEOUT exits all block same-day re-entry only — re-entry
             allowed on the next trading day. (SL cooldown was 7 trading days
             pre-2026-06-13; reduced to 1 to maximise opportunity to re-enter
             after the volatility that triggered the stop has passed.)
  Cutoff:    no auto-entry after 16:30 local (matches patterns/cross).
  Entries:   primary signals AUTO-ENTER at ask on the run that surfaces them
             (2026-08-24), the same way live_patterns does. This reverts the
             2026-05-27 manual-only flow, under which the engine only ever
             surfaced candidates and the user had to approve each one with a
             buy limit price — the wallet sat at zero open positions.
  Approval:  watchlist tickers approved via the UI are one-shot — the approval
             is consumed automatically the first time a position is opened.
             Approval is still required for mid/small caps (order size can
             exceed available volume), it just now buys at market instead of
             waiting for a user-set limit. User-placed limit orders still work
             and still fill in section 4b.

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
from datetime import date, datetime, timezone, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

import env_loader      # noqa: E402
env_loader.load_env()
import engine_common as ec  # noqa: E402
import infoScrapper    # noqa: E402
import tradesScrapper  # noqa: E402
import ohlc_capture    # noqa: E402
import calendar_cache  # noqa: E402
import newsScrapper    # noqa: E402  — shadow news label on new positions
import version as V     # noqa: E402  — app/strategy version stamping
import notify          # noqa: E402  — trade notifications (macOS banner / ntfy push)

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

# Superseded for entries by ENTRY_AT_CLOSE (sig-9, 2026-08-31) — kept because
# past_late_entry_cutoff() is still the honest answer to "would an intraday
# auto-entry have been allowed now", which the pending-signal surfacing uses.
LATE_ENTRY_CUTOFF_MIN = 16 * 60 + 30   # 16:30 local

# ── Close-confirmed entry (sig-9, 2026-08-31) ────────────────────────────────
# Entries fire once per day, after the close, against the confirmed closing
# price — not on the first intraday tick that dips 1% below yesterday's close.
#
# Why: replaying this exact filter over 64 entry days of minute data (2026-04-30
# .. 2026-08-20) with only the execution regime varied:
#
#   close entry + close exit       n=2346   46.6% WR   +0.372%/trade
#   close entry + intraday exit    n=2346   43.5% WR   +0.301%/trade
#   INTRADAY entry + intraday exit n=4352   40.2% WR   +0.202%/trade   <- sig-8
#
# Entering intraday roughly doubles the trade count by taking names that dip 1%
# and recover before the close — trades the backtest never saw — and costs about
# 45% of the per-trade edge. With the wallet slot-constrained rather than
# signal-constrained (6 open positions against a 35k floor), per-trade quality
# is worth more than signal count.
#
# The fill is the official close. Real-world equivalent is a closing-auction
# order; this does carry a mild lookahead (the close both triggers the entry and
# sets the fill), the same assumption the arm-B figure above was measured under.
#
# Timing: the pass rides `last_ohlc_capture_date`, which maybe_capture_eod_ohlc()
# only stamps once the feed has actually advanced past 17:30. That is the same
# hardened settlement gate the EOD sweep uses, and it fires on any post-close
# weekday tick — deliberately NOT a fixed 17:20-17:30 window, which on
# 2026-08-31 saw zero runs (Mac asleep 16:12-18:04) and would have skipped the
# day's entries silently.
ENTRY_AT_CLOSE = True

TP_PCT        = 0.030   # +3.0% — RAISED 2026-09-03 from +2.0% (sig-10).
                        # Winners run well past +2%; clipping there capped the
                        # upside while leaving the -1% downside untouched.
                        # Measured on two independent samples, varying only TP:
                        #   11y close-to-close, 158 large caps, n=68,905
                        #     +2%: 45.8% WR +0.353%/trade | +3%: 41.1% +0.545%
                        #   true tick-order fills, 3.4M minute prints, n=2,723
                        #     +2%: 44.7% WR +0.335%/trade | +3%: 37.4% +0.455%
                        # Monotonic out to +4% on both, but the gain per extra
                        # day of held capital flattens after +3% (per-capital-
                        # day figures under DOD_THRESHOLD). Every one of the 11
                        # years is positive. Expect ~42% WR, i.e. visibly more
                        # red on the page for more money.
SL_PCT        = -0.010  # -1.0% — RESTORED 2026-08-24 to the backtested value.
                        # Widened to -2.0% on 2026-05-25 "to survive opening
                        # volatility" and never re-backtested; that move took
                        # break-even WR from 33.3% to 50.0% and the engine has
                        # run at 43.8% (7W/9L over 16 scored) ever since.
                        # Re-tested 2026-09-03 under true intraday fills and
                        # deliberately LEFT ALONE. On close-only data a tighter
                        # stop looks monotonically better, but that is an
                        # artefact of never seeing an intraday touch; on tick
                        # data the curve is a shallow U — -0.5%: +0.378% |
                        # -1%: +0.335% | -2%: +0.299% | -3%: +0.342% per trade
                        # (n=2,723). The stop level only trades win rate
                        # against loss size. It is not a lever; TP and
                        # DOD_THRESHOLD are.
MAX_HOLD_DAYS = 7
# How far BELOW the stop a stop-out may be booked. min(cur_price, sl) is the
# honest rule across an overnight gap, but the engine only samples every 3 min
# and cur_price is the raw `last` trade of a possibly thin Nordic name: an odd
# lot or a print at a stale bid would otherwise be booked as the exit price of
# the most frequent exit in this book. Wider than any plausible gap on a name
# this engine will size into, tight enough that a bad tick cannot invent a
# five-figure loss. Applied in apply_exit_checks.
MAX_SL_SLIP   = 0.04
COOLDOWN_DAYS = 1       # block re-entry on a ticker for N trading days after an SL exit only (was 7, reduced 2026-06-13 to same-day-only block — re-enter next trading day)
# Block a name already held in the Patterns or Cross wallet, collapsed to the
# company stem. 2026-08-24 closed with Electrolux B here and Electrolux A in
# Patterns — this engine had rejected Electrolux A itself as a duplicate but
# could not see the other books. Trade-off documented in engine_common.
CROSS_WALLET_DEDUPE = True

DOD_THRESHOLD = -0.020  # require day-over-day close <= -2% to enter.
                        # DEEPENED 2026-09-03 from -1% (sig-10). Monotonic on
                        # both samples — only real dislocations mean-revert:
                        #   11y close n=68,905:  -1%: +0.353%/trade (2.46d hold,
                        #     +0.143%/capital-day) | -2%: +0.385% | -3%: +0.423%
                        #   tick fills n=2,723:  -1%: +0.335% | -2%: +0.390%
                        #     | -3%: +0.434%
                        # -2% over -3% deliberately: -3% scores higher per
                        # trade (+0.267%/capital-day vs +0.231%) but fires only
                        # ~3x/day across the 158-name universe against ~6.6x.
                        # This wallet is slot-constrained, not signal-
                        # constrained (6 open, 581k idle cash), so starving it
                        # of candidates costs more than the extra edge earns.
                        # Side note: this also confirms the most-negative-DoD-
                        # first candidate ranking further down is ordered the
                        # right way — deep down-days are the BEST bucket, not
                        # the worst, on both samples.
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
# Upper bound on the close-confirmed ENTRY pass (added 2026-09-08, sig-12).
# Filling at the official close is justified as "the real-world equivalent of a
# closing-auction order" — an order that must be submitted by ~17:25. The gate
# had no upper bound, so the decision could be made arbitrarily late: entries
# fired at 17:45, 18:01, 18:04, 18:05 and — on 2026-09-03 — 19:57, by which
# point the US session had been open ~3.5h and post-close Nordic news had
# landed. That is information no auction order could have had, i.e. lookahead
# beyond the sig-9 measurement. Past this cutoff the day is SKIPPED rather than
# entered late.
#
# An unbounded pass was originally deliberate: a fixed window produced zero
# entries on 2026-08-31, when the Mac slept 16:12-18:04. That reasoning is now
# obsolete — `pmset repeat wakepoweron 17:48 weekdays` plus the plist's 17:50
# Mon-Fri StartCalendarInterval mean the window is reached on its own, and the
# feed settles ~16 min after the close (~17:46), so this leaves ~45 min of
# usable runway against a 3-minute cadence.
#
# Expressed as minutes AFTER THE CLOSE, not as a wall-clock time: on a 13:00
# half-day an absolute 18:30 would have permitted a decision 5.5h after the
# close, which is the exact thing this bound exists to prevent. On an ordinary
# day close+60 == 18:30, i.e. identical to EOD_WINDOW_END.
ENTRY_WINDOW_MINUTES = 60
MARKET_CLOSE     = (17, 30)
# Nasdaq Stockholm half-days close at 13:00 (Maundy Thursday, Jan 5, Apr 30, the
# day before Ascension). Consulted through market_close_for(); see is_early_close.
EARLY_CLOSE      = (13, 0)
# Event-calendar refresh threshold for the EOD sweep. Below 24h on purpose: the
# sweep runs once per trading day at a varying minute (17:46-18:30), so the
# cache's own 24h staleness test skipped the refresh every other day.
EVENT_CAL_REFRESH_HOURS = 12
# Version of the shadow news label stamped into signal_info["news"].
NEWS_LABEL_VERSION = 1

# Stockholm's closing auction prints a few seconds *before* 17:30:00 — the probe
# ticker's final trade of the day lands at 17:29:54. Demanding trade_dt >= exactly
# 17:30:00 therefore never once passed: it silently blocked BOTH the EOD sweep and
# the daily OHLC capture every trading day from 2026-06-13 to 2026-08-18. The old
# log line rendered the shortfall as "0m before close", which read like a rounding
# artefact rather than a hard block. Tolerance is generous because anything printing
# after 17:25 is already an auction/closing print, not continuous-session data.
CLOSE_SETTLE_TOLERANCE = timedelta(minutes=5)

# A position that is still open well past its target exit date means the engine
# was not running to manage it — not that the strategy held it. Booking such a
# position as a clean TP/SL at the synthetic limit price invents a fill that
# never happened: the 2026-06-16..07-28 outage left 9 positions unmanaged for
# ~6 weeks and every one of them was recorded as an exact +/-2.00% TP or SL on
# the day the engines came back. Those are exits at an arbitrary later price, so
# they are booked as STALE at the real market price and excluded from win-rate
# stats. Grace is generous enough to absorb a long weekend plus a holiday.
STALE_EXIT_GRACE_DAYS = 4


# ----------------------------------------------------------------------------- helpers

def load_state():
    return ec.load_state(STATE_PATH, {
        "cash":              WALLET_START,
        "wallet_start":      WALLET_START,
        "positions":         {},
        "closed_trades":     [],
        "last_run":          None,
        "last_universe":     [],
        "last_watchlist":    [],
    })


def save_state(state):
    ec.save_state(STATE_PATH, state, in_session=is_market_open())


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


def reconcile_force_sells(state, closed_now, force_sells, path=None, engine="signals"):
    """Consume the force-sell queue, INCLUDING entries that can never fire.

    A ticker only ever left the queue when apply_exit_checks actually booked a
    MANUAL exit for it. A queued ticker with no open position — the user pressing
    "Sell now" on a row rendered moments before the post-close tick closed it —
    therefore closed nothing, consumed nothing, and sat in
    state/live_*_force_sell.json forever, invisible (the UI draws the "queued"
    badge on an open-position row, and an orphan has no row). Force-sell is the
    FIRST branch of the exit chain, so the next time that ticker was re-entered,
    the very next 3-minute tick liquidated the fresh position at market — days
    later, with nothing in any log saying why.

    Orphans are pruned here and NAMED in the log: a dropped intent must be
    visible, never silent. A position still open but deferred for want of a live
    quote (see apply_exit_checks) is NOT an orphan and stays queued.
    """
    sold    = {t["ticker"] for t in closed_now if t["reason"] == "MANUAL"}
    orphans = set(force_sells) - sold - set(state["positions"])
    if orphans:
        print(f"   pruned {len(orphans)} force-sell request(s) with no open "
              f"{engine} position: {', '.join(sorted(orphans))}")
    consume_force_sells(sold | orphans, path=path)


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
    """`n` real sessions forward — holidays skipped, not counted (see
    is_trading_day). Counting them made a MAX_HOLD_DAYS=7 hold as short as 4
    actual sessions over Easter, and could land target_exit_date on a day the
    exchange is shut, where `today >= target_date` books a TIMEOUT at a price
    that never traded."""
    d = start_date
    added = 0
    while added < n:
        d += timedelta(days=1)
        if is_trading_day(d):
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
        if is_trading_day(d):
            days += 1
    return days


def signal_check(closes, today_date=None):
    """VALIDATED mean-reversion entry filter.

    All four conditions must be true to pass:
      1) Day-over-day return <= DOD_THRESHOLD (today closed >=2% below yesterday)
      2) Today's close is NOT at the 5-day high   [UNREACHABLE — see below]
      3) Today is not a Friday        (no carry over the weekend)
      4) 20-day return > -15%         (crash guard)

    closes      — list of closes ending with today's value (live quote tacked on)
    today_date  — optional datetime.date for the Friday check. If None, today is
                  derived from datetime.now() in the caller's tz; this matches
                  how the live engine invokes this function during market hours.

    Condition 2 can never fire and is kept only as documentation of intent.
    Condition 1 already requires today to have closed below yesterday, and
    yesterday sits inside the 5-day window, so `today_p >= hi5` is
    unreachable for any passing candidate. Confirmed empirically on
    2026-09-03: an 11-year 158-name backtest returns byte-identical results
    with the check on and off (n=68,905 either way). Left in place because
    removing it would change nothing and it documents the original filter.
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


def past_late_entry_cutoff():
    return ec.past_cutoff(LATE_ENTRY_CUTOFF_MIN)


def close_confirmed_entry_ready(state, today):
    """(ready, reason, missed_window) for today's post-close entry pass.

    Gated on `last_ohlc_capture_date` rather than on the wall clock: capture
    only stamps that date after probe_data_lag() shows the feed past today's
    17:30 close, so piggybacking on it means the entry pass cannot run against
    quotes that are still mid-session. `reason` is persisted so a day with no
    entries always says why on the page instead of looking like "no signals".

    `missed_window` is True only for the ENTRY_WINDOW_MINUTES skip — a trading day
    on which entries were possible and were forfeited. The page paints that as
    a warning; every other closed reason is routine and stays muted.
    """
    if not ENTRY_AT_CLOSE:
        return False, "close-confirmed entry disabled", False
    iso = today.isoformat()
    if state.get("last_entry_scan_date") == iso:
        return False, "already scanned after today's close", False
    now_local = datetime.now()
    if not is_trading_day(now_local):
        # Named apart so the page can tell a Saturday from Epiphany — both used
        # to render as the generic "weekend".
        return False, ("weekend" if now_local.weekday() >= 5 else "market holiday"), False
    close_h, close_m = market_close_for(now_local)
    mins_now = now_local.hour * 60 + now_local.minute
    if mins_now < close_h * 60 + close_m:
        half = " half-day" if is_early_close(now_local) else ""
        return False, f"before the {close_h:02d}:{close_m:02d}{half} close", False
    # Past the cutoff the day is skipped, not entered late — checked BEFORE the
    # feed-settled test so a feed that only settles at 19:00 reports the real
    # reason (window gone) rather than blaming the feed. `missed` is what the
    # page escalates on: an ordinary closed gate renders as a muted line, and a
    # skipped trading day must not look like "already scanned".
    cutoff = close_h * 60 + close_m + ENTRY_WINDOW_MINUTES
    if mins_now > cutoff:
        half = " half-day" if is_early_close(now_local) else ""
        return False, (f"past the {cutoff // 60:02d}:{cutoff % 60:02d} entry cutoff "
                       f"({ENTRY_WINDOW_MINUTES} min after the "
                       f"{close_h:02d}:{close_m:02d}{half} close) — no entries today"), True
    if state.get("last_ohlc_capture_date") != iso:
        why = "feed has not settled past today's close yet"
        if is_early_close(now_local):
            why += " (13:00 half-day)"
        return False, why, False
    return True, "", False


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


# ----------------------------------------------------------------------------- trading calendar

# Nasdaq Stockholm non-trading days. Every date gate in the three engines used
# to decide "trading day" with `weekday() < 5` alone, which counts Epiphany,
# Good Friday, Midsummer Eve and the rest as ordinary sessions. Three live
# consequences: is_market_open() was True on a shut weekday, so live_patterns
# ran a full intraday pass — auto-entries included — against quotes that were
# only the previous session's close; add_trading_days() counted holidays as
# sessions, so the validated 7-day hold could be 4 real sessions over Easter;
# and COOLDOWN_DAYS / STALE_EXIT_GRACE_DAYS miscounted the same way, expiring
# an SL cooldown without a session having traded.
#
# Derived from Easter rather than pasted per year, so the table cannot silently
# expire the way a hardcoded 2026 list would.

_HOLIDAY_CACHE = {}


def easter_sunday(year):
    """Anonymous Gregorian algorithm — four Stockholm closures hang off Easter."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    p = (32 + 2 * e + 2 * i - h - k) % 7
    q = (a + 11 * h + 22 * p) // 451
    n = h + p - 7 * q + 114
    return date(year, n // 31, n % 31 + 1)


def market_holidays(year):
    """Dates the Stockholm exchange is shut for the whole day."""
    cached = _HOLIDAY_CACHE.get(year)
    if cached is not None:
        return cached
    easter = easter_sunday(year)
    # Midsummer Eve is the Friday inside Jun 19-25 (Midsummer Day is always the
    # Saturday between Jun 20 and Jun 26), so it moves every year.
    midsummer_eve = next(date(year, 6, d) for d in range(19, 26)
                         if date(year, 6, d).weekday() == 4)
    days = frozenset({
        date(year, 1, 1),               # Nyårsdagen
        date(year, 1, 6),               # Trettondedag jul
        easter - timedelta(days=2),     # Långfredagen
        easter + timedelta(days=1),     # Annandag påsk
        date(year, 5, 1),               # Första maj
        easter + timedelta(days=39),    # Kristi himmelsfärdsdag
        date(year, 6, 6),               # Nationaldagen
        midsummer_eve,                  # Midsommarafton
        date(year, 12, 24),             # Julafton
        date(year, 12, 25),
        date(year, 12, 26),
        date(year, 12, 31),             # Nyårsafton
    })
    _HOLIDAY_CACHE[year] = days
    return days


def is_trading_day(d):
    """A weekday the Stockholm exchange actually opens."""
    if isinstance(d, datetime):
        d = d.date()
    return d.weekday() < 5 and d not in market_holidays(d.year)


def is_early_close(d):
    """True on a 13:00 half-day.

    Load-bearing: market_close_for() below moves the settle threshold and the
    wall-clock gates onto 13:00 for these dates. That is the whole fix — with a
    hardcoded 17:30 close the half-day's last print (~12:59) could never reach
    the 17:25 threshold, so capture retried every 3 minutes until midnight, the
    bar was left permanently missing from ohlc/, and close_confirmed_entry_ready
    reported "feed has not settled past today's close yet" all day for Signals
    AND for Cross (which reads the same key).

    The flip side: a WRONG date here would let a perfectly normal session be
    captured and traded at 13:05 on mid-session prices. Only add a date that is
    genuinely a Stockholm half-day.

    Checked 2026-10-26 against the 2026 Stockholm trading calendar (iSEC
    Handelskalender Sverige 2026, which lists exactly Jan 5, Apr 2, Apr 30,
    May 13 and Oct 30 as half-days). Alla helgons afton was missing until then:
    2026-10-30 would have run as a full session — no capture, no EOD sweep, no
    close-confirmed entries, and 13:00-17:30 treated as live trading.
    """
    if isinstance(d, datetime):
        d = d.date()
    easter = easter_sunday(d.year)
    # Sweden keeps All Saints' Day on the Saturday between Oct 31 and Nov 6, so
    # its eve is always the Friday between Oct 30 and Nov 5.
    oct30 = date(d.year, 10, 30)
    all_saints_eve = next(oct30 + timedelta(days=i) for i in range(7)
                          if (oct30 + timedelta(days=i)).weekday() == 4)
    return d in {
        date(d.year, 1, 5),             # day before Epiphany
        easter - timedelta(days=3),     # Skärtorsdagen
        date(d.year, 4, 30),            # Valborgsmässoafton
        easter + timedelta(days=38),    # day before Kristi himmelsfärdsdag
        all_saints_eve,                 # Alla helgons afton
    }


def market_close_for(d):
    """(hour, minute) the exchange closes on date `d` — 13:00 on a half-day.

    Every close gate used the MARKET_CLOSE constant, which is only true on a
    full session; see is_early_close() for what a hardcoded 17:30 did to the
    four half-days a year.
    """
    return EARLY_CLOSE if is_early_close(d) else MARKET_CLOSE


def is_market_open(now_local=None):
    if now_local is None:
        now_local = datetime.now()
    if not is_trading_day(now_local):
        return False
    close_h, close_m = market_close_for(now_local)
    mins = now_local.hour * 60 + now_local.minute
    return 9 * 60 <= mins <= close_h * 60 + close_m


def is_in_eod_window(now_local=None):
    if now_local is None:
        now_local = datetime.now()
    if not is_trading_day(now_local):
        return False
    mins  = now_local.hour * 60 + now_local.minute
    start = EOD_WINDOW_START[0] * 60 + EOD_WINDOW_START[1]
    end   = EOD_WINDOW_END[0]   * 60 + EOD_WINDOW_END[1]
    return start <= mins <= end


def eod_sweep_due(state, today, now_local=None):
    """Is today's post-close exit sweep still owed?

    Was `is_in_eod_window()`, a pure 17:45-18:30 wall-clock test: if the Mac
    was asleep, launchd was throttled or the feed was still behind at 18:30,
    the gate closed for the rest of the day and that day's positions were never
    checked against real closing prices — silently, since last_eod_sweep_date
    simply stayed on yesterday. Same narrow-window shape as the capture
    incident, fixed the same way (2026-06-13): any post-close trading-day tick
    may run it, and run_eod_sweep's own probe — feed_settled_past_close(),
    anchored on TODAY's close — decides whether the prints it would fill
    against are real, bailing with "retry next run" until they are. Idempotent
    on last_eod_sweep_date, which the sweep stamps only once it has run.
    EOD_WINDOW_START/END now only describe when this is EXPECTED to happen.
    """
    if state.get("last_eod_sweep_date") == today.isoformat():
        return False
    if now_local is None:
        now_local = datetime.now()
    if not is_trading_day(now_local):
        return False
    close_h, close_m = market_close_for(now_local)
    return now_local.hour * 60 + now_local.minute >= close_h * 60 + close_m


def feed_settled_past_close(trade_dt, today=None):
    """Has the feed advanced far enough to be showing TODAY's closing prints?

    Returns (settled, reason). `reason` is a ready-to-print phrase for the retry
    log line and for the persisted entry_gate, and is "" when settled. The
    shortfall is reported in seconds under a minute so a near-miss can never
    again look like a rounding artefact — see CLOSE_SETTLE_TOLERANCE.

    The threshold used to be built from `trade_dt` itself, which answered "was
    this trade near the close of ITS OWN day" — so a trade from ANY previous
    session at/after 17:25 passed. maybe_capture_eod_ohlc happened to pre-check
    the date; run_eod_sweep did not, so a frozen or day-stale feed let the sweep
    book TP/SL/TIMEOUT exits against yesterday's prices and then stamp
    last_eod_sweep_date so the real close was never swept. The threshold is now
    built from `today`, and a stale-dated print can never satisfy it.
    """
    if today is None:
        today = ec.today_local()
    if trade_dt.date() != today:
        return False, (f"feed still on {trade_dt.date().isoformat()} "
                       f"(today {today.isoformat()})")
    close_h, close_m = market_close_for(today)
    close_time = trade_dt.replace(hour=close_h, minute=close_m,
                                  second=0, microsecond=0)
    threshold = close_time - CLOSE_SETTLE_TOLERANCE
    if trade_dt >= threshold:
        return True, ""
    secs = (threshold - trade_dt).total_seconds()
    short = f"{int(secs)}s" if secs < 60 else f"{secs / 60:.1f}m"
    half  = " half-day" if is_early_close(today) else ""
    return False, (f"feed only at {trade_dt.strftime('%H:%M:%S')} — {short} short "
                   f"of the {close_h:02d}:{close_m:02d}{half} close threshold")


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
            # Column 3 explicitly, never row[-1]: stock_names.csv gained sector
            # columns (4,5) on 2026-08-31. Reading the last column here would
            # have matched the sector name against the tier whitelist below and
            # silently emptied the universe for all three engines.
            insref_raw, name, tier = row[0], row[1], row[3].strip()
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
            # Day's cumulative share volume — the liquidity gate's fallback when
            # a ticker has no captured OHLC history. Already requested by
            # infoScrapper's _QUOTE_FIELDS, so this is free.
            "quantity": q.get("quantity"),
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


def hold_duration(entry_ts, exit_ts):
    """"19h 35m" / "2d" — the notification's counterpart to closedHold() in
    web/live_common.js. Both stamps are local wall-clock with no zone, so they
    are parsed with the same naive format and subtracted."""
    try:
        a = datetime.fromisoformat(str(entry_ts))
        b = datetime.fromisoformat(str(exit_ts))
    except (TypeError, ValueError):
        return None
    sec = max(0, int((b - a).total_seconds()))
    if sec >= 86400:
        return f"{round(sec / 86400)}d"
    if sec >= 3600:
        return f"{sec // 3600}h {(sec % 3600) // 60}m"
    if sec >= 60:
        return f"{sec // 60}m"
    return f"{sec}s"


def apply_exit_checks(state, today, now_local_ts, live, force_sells=None,
                      engine="signals"):
    """Close any open positions whose current live price triggered TP/SL/timeout.
    Also honours a user-requested manual exit set: tickers in `force_sells`
    are sold at the current live price (no limit-fill cap — the user pressed
    "Sell now"), recorded with reason='MANUAL'. Force-sell takes priority
    over TP/SL/TIMEOUT so a position queued for exit gets sold regardless of
    where the price is — the one exception being a position with no live quote
    at all, whose sell is DEFERRED (kept queued, logged, and listed in
    state["force_sell_blocked"]) rather than filled at a synthetic price.
    Mutates state and returns the list of trades closed.

    `engine` only labels the notification — live_patterns and live_cross both
    call this with their own key, since this is the one place all three engines
    book a sell."""
    if force_sells is None:
        force_sells = set()
    closed_now = []
    deferred_sells = []          # queued MANUAL exits this run could not fill
    for tk in list(state["positions"].keys()):
        pos = state["positions"][tk]
        lq = live.get(tk, {})
        cur_price = lq.get("last")
        # csv-fallback / stale = the live fetch failed and we substituted a
        # synthetic price (last CSV row or entry price). Never trigger price-
        # based exits on a non-live quote — wait for the next good run.
        if cur_price is None or lq.get("source") in ("csv-fallback", "stale"):
            # MANUAL is not price-triggered, but it still needs a real price to
            # fill at, so it is DEFERRED — and said out loud. This guard sat
            # ABOVE the force_sells branch, so a position whose quote fetch kept
            # failing (the thin-name case) swallowed the user's "Sell now"
            # silently: the button stayed "queued", the engine no-op'd every 3
            # minutes and no log line ever named the position. A blocked intent
            # has to be visible — see the standing rule on silent gates.
            if tk in force_sells:
                why = ("no quote at all this run" if cur_price is None
                       else f"only a {lq.get('source')} price")
                print(f"   MANUAL sell for {tk} DEFERRED — {why}; stays queued "
                      f"for the next run with a live quote")
                deferred_sells.append({"ticker": tk, "reason": why,
                                       "source": lq.get("source"), "ts": now_local_ts})
            continue
        target_date = (datetime.fromisoformat(pos["target_exit_date"]).date()
                       if isinstance(pos["target_exit_date"], str)
                       else pos["target_exit_date"])

        # A stop moved *after* entry (scripts/retighten_stops.py, used when
        # SL_PCT changed while positions were open) may sit below where the
        # market already is. That case is now covered by the general min() rule
        # in the SL branch below; the marker is still cleared here so a stale
        # flag cannot outlive the condition it described.
        if pos.get("sl_fill_at_market") and cur_price > pos["sl"]:
            pos.pop("sl_fill_at_market", None)

        exit_reason = None
        fill_price = cur_price
        days_past = (today - target_date).days
        if tk in force_sells:
            exit_reason = "MANUAL"   # user-requested sell-now via UI
            fill_price = cur_price   # market-style fill at current price
        elif days_past > STALE_EXIT_GRACE_DAYS:
            # Engine was down through this position's exit — see STALE_EXIT_GRACE_DAYS.
            exit_reason = "STALE"
            fill_price = cur_price   # real market price, never a synthetic TP/SL fill
        elif cur_price >= pos["tp"]:
            exit_reason = "TP"
            fill_price = pos["tp"]   # limit-sell @ TP — fills at TP, not above
        elif cur_price <= pos["sl"]:
            exit_reason = "SL"
            # A stop can never fill BETTER than the market. Booking pos["sl"]
            # whenever the price was at or under it assumed the stop was always
            # touched on the way down — but under ENTRY_AT_CLOSE the engine's
            # next look at a position is the following session's ~09:00 tick, so
            # EVERY first stop test is taken across an overnight gap it never
            # observed. On a gap-down open the stop price simply never traded and
            # the fill was invented money: all 81 stop-outs in the three books
            # closed at exactly -1.00% / -2.00%, while 10 of them opened BELOW
            # their stop (~4,100 SEK of phantom P&L, and a flattered loss tail in
            # the -EV analysis). min() is the honest rule, and it subsumes the
            # sl_fill_at_market special case retighten_stops.py needed.
            # Floored at MAX_SL_SLIP: ~87% of stop-outs are seen mid-session,
            # where cur_price is a raw single print 0-3 min after the breach,
            # not an opening auction — one odd lot must not book a -7% exit
            # against a -1% stop and freeze it in closed_trades.
            fill_price = max(min(cur_price, pos["sl"]),
                             pos["sl"] * (1 - MAX_SL_SLIP))
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
                "unmanaged":   exit_reason == "STALE",
                "days_past_target": days_past if exit_reason == "STALE" else None,
                "entry_tag":   (pos.get("signal_info") or {}).get("entry_tag", "standard"),
                "conviction":  (pos.get("signal_info") or {}).get("conviction", "normal"),
                "signal_info": pos.get("signal_info"),
                # Carried from the position, not re-read from the ledger: a trade
                # must score against the strategy that *opened* it, even if the
                # parameters were bumped while it was still open.
                "app_version":      pos.get("app_version"),
                "strategy_version": pos.get("strategy_version"),
                "version_source":   pos.get("version_source"),
            }
            state["closed_trades"].append(closed)
            closed_now.append(closed)
            del state["positions"][tk]
            # Queued, not sent: the wallet has not been written yet. Announcing
            # a sell that a crash then rolls back is worse than a missed
            # banner — see the delivery model in lib/notify.py.
            notify.queue_sell(engine, closed,
                              hold=hold_duration(closed.get("entry_ts"), now_local_ts))
    # Always rewritten (empty included) so a cleared block cannot linger on the
    # page under a fresh timestamp — the stale-behind-a-fresh-clock trap.
    state["force_sell_blocked"] = deferred_sells
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
      · skip on weekends and Stockholm holidays (no close to capture)
      · skip if local clock is still before the day's close (17:30, or 13:00
        on a half-day — see market_close_for)
      · skip if the feed probe (SEB A trade timestamp) fails outright
      · skip if the feed trade is from a different date than `today`, or is
        still before today's close — both now answered by
        feed_settled_past_close(), which guards against writing today's bar
        from yesterday's OHLC fields
    Any skip leaves `last_ohlc_capture_date` unset so the next tick retries.

    Any weekday tick after 17:30 (inside or outside the EOD window) will
    capture once the feed has settled. Missing the narrow 17:45-18:30
    window no longer means missing the day.
    """
    if state.get("last_ohlc_capture_date") == today.isoformat():
        return

    now_local = datetime.now()
    if not is_trading_day(now_local):
        return
    mins_now = now_local.hour * 60 + now_local.minute
    close_h, close_m = market_close_for(now_local)
    if mins_now < close_h * 60 + close_m:
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
    settled, why = feed_settled_past_close(trade_dt, today)
    if not settled:
        print(f" OHLC capture: {why}; retry next run")
        return

    primary, _ = build_universe()
    n_ohlc = ohlc_capture.capture_eod_ohlc(today, primary, fetch_live_parallel)
    state["last_ohlc_capture_date"] = today.isoformat()
    state["ohlc_bars_total"] = n_ohlc
    print(f" OHLC capture: wrote {n_ohlc} bars for {today.isoformat()} → ./ohlc/")
    save_state(state)


def refresh_event_calendar():
    """Re-pull the corporate-event calendar for the primary universe once it is
    more than EVENT_CAL_REFRESH_HOURS old, so the next day's entries see the
    latest dividends/earnings. Every completed EOD sweep leaves a refresh owed;
    main() pays it, wallet on disk, in the run that settles the day's entry
    question (see take_event_cal_refresh). A failure only leaves the cache stale
    (shown on /live-health) and never reaches the caller."""
    try:
        events_cache = calendar_cache.load_cache()
        if not calendar_cache.is_stale(events_cache, hours=EVENT_CAL_REFRESH_HOURS):
            print("   event calendar still fresh, skipping refresh")
            return
        primary, _ = build_universe()
        print(f"   refreshing event calendar for {len(primary)} stocks…")
        calendar_cache.refresh_cache([t["insref"] for t in primary])
        print("   event calendar refreshed")
    except Exception as e:
        print(f"   ✗ event calendar refresh failed ({e}); cache left as it was")


def take_event_cal_refresh(state, today, entry_settled):
    """True — and the owed flag cleared — when today's completed EOD sweep left
    an event-calendar refresh owed and the day's entry question is settled (the
    close-confirmed pass ran, was missed, or cannot happen today). The refresh is
    158 sequential fetches (~1 min, far longer on timeouts); run ahead of a pass
    still to come it delayed the close+60 entry gate and could forfeit the day's
    entries. The flag lives in state, so a run that crashes between the sweep and
    the pass still pays it on the retry."""
    if not entry_settled or state.get("event_cal_refresh_owed") != today.isoformat():
        return False
    state.pop("event_cal_refresh_owed", None)
    return True


def shadow_news_label(insref, today):
    """Same-day company news for a new position, stamped into its signal_info
    (and from there into closed_trades) so news-vs-outcome can be measured on
    live trades. Shadow only: it never gates or sizes an entry — a 12-month
    backtest found no news veto that improves this strategy (CHANGELOG 0.25.0).

    Window: the previous session's close to 5 minutes before today's close, i.e.
    what a closing-auction order could have known. Items that land after that
    (the entry pass runs ~17:46 or later) are left out on purpose."""
    prev = today - timedelta(days=1)
    while not is_trading_day(prev):
        prev -= timedelta(days=1)
    ph, pm = market_close_for(prev)
    th, tm = market_close_for(today)
    start = datetime(prev.year, prev.month, prev.day, ph, pm)
    end = datetime(today.year, today.month, today.day, th, tm) - timedelta(minutes=5)
    label = newsScrapper.news_window_summary(insref, start, end)
    label["v"] = NEWS_LABEL_VERSION
    label["window"] = f"{start:%Y-%m-%d %H:%M} -> {end:%Y-%m-%d %H:%M}"
    return label


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

    settled, why = feed_settled_past_close(trade_dt, today)
    if not settled:
        print(f"   {why}; retry next run")
        save_state(state)
        return
    print(f"   feed settled (last {lag['ticker']} trade @ "
          f"{trade_dt.strftime('%H:%M:%S')})")

    if not state["positions"]:
        print("   no open positions to check")
        state["last_eod_sweep_date"] = today.isoformat()
        state["event_cal_refresh_owed"] = today.isoformat()
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

    # Wallet first, queue second. The exits above exist only in memory until
    # save_state, yet the queue file used to be emptied here while the wallet was
    # not written until the end of this function — with build_universe() and,
    # once a day, a 158-fetch calendar refresh in between. Any abort in that gap
    # destroyed the user's intent WITHOUT executing it (position still open,
    # "queued" badge gone) while notify's atexit hook still pushed the "sold"
    # that had just been rolled back. Saving first inverts the failure into a
    # harmless one: the exit is booked and the queue may be consumed again next
    # run — apply_exit_checks simply skips a ticker that is no longer open.
    save_state(state)
    reconcile_force_sells(state, closed_now, force_sells)

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

    state["last_eod_sweep_date"] = today.isoformat()
    state["event_cal_refresh_owed"] = today.isoformat()
    save_state(state)

    # The event-calendar refresh used to run here, and only here, so a flat
    # wallet (the early return above) skipped it: the cache that gates all three
    # engines was 10 days old on 2026-10-26. Both paths now leave a refresh owed,
    # which main() pays once the day's entry question is settled — see
    # take_event_cal_refresh(). (OHLC capture moved to maybe_capture_eod_ohlc(),
    # a self-gated daily step in main().)


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
        # Not gated on the 17:45-18:30 clock window any more — see
        # eod_sweep_due(); the feed probe inside the sweep is the real gate.
        if eod_sweep_due(state, today):
            run_eod_sweep(state, today, now_local_ts)

        # sig-9: entries are close-confirmed, so the post-close tick falls
        # THROUGH into the ordinary scan below instead of returning. Exits are
        # applied first (run_eod_sweep above, then apply_exit_checks in step 3),
        # so a name stopped out at today's close lands in the cooldown set
        # before the entry scan can look at it.
        ready, why, missed_window = close_confirmed_entry_ready(state, today)
        if not ready:
            # Written to BOTH keys: the pages read `last_entry_gate ||
            # entry_gate`, so writing only one renders the previous run's
            # reason under this run's fresh timestamp — the stale-behind-a-
            # fresh-clock trap. No last_skipped touched here: this wallet does
            # not publish one (see the end of main), and this branch is the one
            # that fires on EVERY tick after the day's close-confirmed pass, so
            # a clear here erases the day's list minutes after it is written.
            gate = {"open": False, "reason": why,
                    "date": today.isoformat(), "checked": now_local_ts,
                    "missed_window": missed_window}
            state["entry_gate"] = gate
            state["last_entry_gate"] = gate
            # Settled = no close-confirmed pass is still to come today. On the
            # transient reasons (before the close, feed not settled) the pass is
            # yet to run, so an owed calendar refresh keeps waiting for it.
            entry_settled = (missed_window or not ENTRY_AT_CLOSE
                             or not is_trading_day(today)
                             or state.get("last_entry_scan_date") == today.isoformat())
            pay_refresh = take_event_cal_refresh(state, today, entry_settled)
            save_state(state)
            print(f" Market closed (Stockholm OMX 09:00-17:30 Mon-Fri). "
                  f"No entry pass: {why}.")
            if pay_refresh:
                refresh_event_calendar()
            return
        print(f" CLOSE-CONFIRMED ENTRY PASS — feed settled past today's close, "
              f"scanning at closing prices")
        close_entry_pass = True
    else:
        close_entry_pass = False

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
    # Wallet first, queue second — see the note in run_eod_sweep. The scan below
    # runs for minutes before this run reaches its own save_state, and a crash in
    # there used to lose the sell while the atexit push announced it anyway.
    save_state(state)
    # One-shot semantics: drop the tickers actually sold, plus any queued ticker
    # that no longer has a position to sell.
    reconcile_force_sells(state, closed_now, force_sells)

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
    # Cross is deliberately excluded from the sibling set. Its dedupe flag is
    # off on its own side (live_cross.CROSS_WALLET_DEDUPE = False) because every
    # Cross candidate is BY CONSTRUCTION a name that passed this engine's
    # signal_check that day — that is the x-4 gate — so the collision rate is
    # 100% by design. Reading Cross's positions here made the check effectively
    # bidirectional: the experimental arm silently deleted entries from the
    # benchmark arm it is measured against, for up to MAX_HOLD_DAYS per Cross
    # position, and not at random (Cross enters precisely on the days Signals
    # could not). That is the capacity bias the separate Cross wallet and its
    # 500k top-up exist to prevent. Patterns' book still blocks — independent
    # setups, genuine share-class duplication.
    # Cost, recorded here: the same company can now be held by Signals and Cross
    # at once, so aggregate cross-wallet concentration is not capped by this gate.
    # Keyed off ec.WALLET_FILES, never a repeated literal: a rename there would
    # silently stop matching and put Cross back in the dedupe set — failing soft
    # in the wrong direction, re-enabling a gate deliberately turned off.
    cross_wallet_file = ec.WALLET_FILES["cross"][1]
    sibling_paths = [(label, path) for label, path in ec.sibling_wallets("signals")
                     if os.path.basename(path) != cross_wallet_file]
    foreign_stems = (ec.foreign_held_stems(sibling_paths, company_stem)
                     if CROSS_WALLET_DEDUPE else {})
    pending         = []   # cleared every gate but could not be sized — UI shows them
    auto_entries    = []   # opened at market by this run
    skipped_signals = []   # informational skips surfaced in the UI
    consumed_approvals    = set()
    consumed_force_entries = set()
    # Load the corporate-event cache once per run — used to block entries
    # near ex-dividend dates / earnings reports (added 2026-05-25 after
    # Tele2 A was bought into a 6-days-stale ex-dividend tick).
    events_cache = calendar_cache.load_cache()
    # AUTO-ENTRY flow (2026-08-24): a candidate that clears every gate is bought
    # at ask on this run, mirroring live_patterns. Between 2026-05-27 and now
    # this was manual-only — candidates were surfaced as *pending* and the user
    # had to approve each one with a buy limit price; the result was a wallet
    # holding zero open positions. Anything that clears the gates but cannot be
    # sized (cash below the floor, or below POS_MIN after rounding) still lands
    # in `pending` so the UI keeps showing it as actionable. User-placed limit
    # orders are untouched and still fill in section 4b.
    # After 16:30 nothing new is bought, but candidates still surface so the
    # user can place a limit order by hand.
    # sig-9: an intraday run never buys. It scans, applies exits and surfaces
    # candidates as `pending` so the page still shows what is building during
    # the session; only the post-close pass converts a candidate into a fill.
    entries_open = close_entry_pass
    if not entries_open:
        print(f"   · intraday run ({datetime.now().strftime('%H:%M')}) — candidates surface "
              f"as pending; entries fire after the close (sig-9)")

    def surface_pending(t, lq, info, price, reason):
        pending.append({
            "ticker":          t["name"],
            "insref":          t["insref"],
            "last":            lq.get("last"),
            "bid":             lq.get("bid"),
            "ask":             lq.get("ask"),
            "suggested_price": price,
            "tp_preview":      round(price * (1 + TP_PCT), 4),
            "sl_preview":      round(price * (1 + SL_PCT), 4),
            "info":            info,
            "from_watchlist":  bool(t.get("from_watchlist")),
            "forced":          bool(info.get("forced")),
            "not_taken":       reason,
        })

    news_label_down = False   # first news-API outage this run skips later labels
    for c in candidates:
        t, lq, info, live_close = c["t"], c["lq"], c["info"], c["live_close"]
        name = t["name"]
        stem = company_stem(name)
        if stem in held_stems:
            skipped_signals.append({"ticker": name, "skipped": f"duplicate of held '{stem}'"})
            continue
        if stem in foreign_stems:
            skipped_signals.append({"ticker": name,
                                    "skipped": f"held in another wallet ({foreign_stems[stem]})"})
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
        # sig-9: the close-confirmed pass fills at the confirmed CLOSE, not the
        # ask. After 17:30 the ask is a stale end-of-session quote that can sit
        # well away from where the stock actually settled — the first dry run of
        # this pass filled BONESUPPORT at its 220.20 ask against a 218.60 close,
        # booking an instant -0.73% the arm-B measurement never assumed. The
        # intraday path keeps the ask because that is a real, executable offer.
        entry_price = live_close if close_entry_pass else (lq.get("ask") or live_close)
        if not entry_price:
            skipped_signals.append({"ticker": name, "skipped": "no live quote"})
            continue
        if not entries_open:
            surface_pending(t, lq, info, entry_price,
                            "awaiting close confirmation")
            continue

        conviction  = info.get("conviction", "normal")
        target, cap = CONVICTION_SIZING.get(conviction, CONVICTION_SIZING["normal"])
        # Liquidity gate: a fill is only credible as a small slice of what the
        # name actually trades. Caps the position rather than only rejecting it,
        # so a mid-liquidity ticker can still take a smaller size.
        liq = ec.liquidity_verdict(t["insref"], entry_price, lq)
        cap, liq_skip = ec.apply_liquidity_cap(liq, cap, POS_MIN)
        if liq_skip:
            skipped_signals.append({"ticker": name, "skipped": liq_skip})
            continue
        shares, cost = ec.position_size(state["cash"], entry_price, target, cap, POS_MIN)
        if shares <= 0:
            # Cleared every gate but unaffordable at this price. Not a skip —
            # the user can still place a smaller limit order by hand, so it
            # stays in `pending` where the UI renders it as actionable.
            surface_pending(t, lq, info, entry_price,
                            f"size <{POS_MIN/1000:.0f}k after rounding")
            continue

        entry_info = dict(info)
        entry_info.setdefault("conviction", "normal")
        if t.get("from_watchlist"):
            entry_info["from_watchlist"] = True
        if info.get("forced"):
            entry_info["forced"] = True
            entry_info["entry_tag"] = "forced"
        entry_info.setdefault("entry_tag", "auto")
        # Shadow news label (0.25.0): recorded for later evaluation only, never
        # a gate — a failed lookup is stamped as an error and the entry proceeds.
        # After one outage the rest of the pass skips the lookup, so a slow news
        # API cannot stack ~5 s timeouts across every entry.
        if news_label_down:
            entry_info["news"] = {"error": "skipped: news API failed earlier this run",
                                  "v": NEWS_LABEL_VERSION}
        else:
            try:
                entry_info["news"] = shadow_news_label(t["insref"], today)
            except Exception as e:
                entry_info["news"] = {"error": str(e)[:200], "outage": True,
                                      "v": NEWS_LABEL_VERSION}
            news_label_down = bool(entry_info["news"].get("outage"))

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
            "signal_info":      entry_info,
            # "close" = filled at the confirmed official close by the sig-9
            # post-close pass; "auto" was the sig-6..sig-8 intraday ask fill.
            "fill_type":        "close" if close_entry_pass else "auto",
            **V.stamp("signals"),
        }
        held_stems.add(stem)
        if t.get("from_watchlist"):
            consumed_approvals.add(name)
        if info.get("forced"):
            consumed_force_entries.add(name)
        auto_entries.append({
            "ticker":  name, "entry": entry_price, "limit": None,
            "market":  lq.get("last"), "tp": tp_price, "sl": sl_price,
            "shares":  shares, "cost": cost,
            "exit_by": exit_by.isoformat(), "info": entry_info,
        })
        notify.queue_buy("signals", name, entry_price, shares, cost=cost,
                         tp=tp_price, sl=sl_price, exit_by=exit_by.isoformat())

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
        if stem in foreign_stems:
            orders_to_remove.add(tk)          # another wallet holds it — cancel
            skipped_signals.append({"ticker": tk,
                                    "skipped": f"limit order cancelled — held in another wallet ({foreign_stems[stem]})"})
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
        liq = ec.liquidity_verdict(o.get("insref") or (lq.get("insref") if lq else None),
                                   limit_price, lq)
        cap, liq_skip = ec.apply_liquidity_cap(liq, cap, POS_MIN)
        if liq_skip:
            orders_to_remove.add(tk)          # too thin to fill at any size we'd hold
            skipped_signals.append({"ticker": tk, "skipped": f"limit order cancelled — {liq_skip}"})
            continue
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
            # Which build + which strategy generation opened this trade. The
            # strategy version is the grouping key the "By version" panels use;
            # see lib/version.py for why a single global version won't do.
            **V.stamp("signals"),
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
        notify.queue_buy("signals", tk, limit_price, shares, cost=cost,
                         tp=tp_price, sl=sl_price, exit_by=exit_by.isoformat(),
                         pattern="your limit order filled")

    # Wallet before queues again: the entries/fills above are still in-memory
    # only, and consume_* rewrites the intent files immediately.
    save_state(state)
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

    if auto_entries:
        print(f"\n--- AUTO ENTRIES THIS RUN ({len(auto_entries)}) ---")
        for s in auto_entries:
            tag_str = format_signal_tag(s["info"])
            print(f"  BUY {tag_str:<10} {s['ticker']:<18}  {s['shares']:>5} sh @ {s['entry']:.2f}  "
                  f"= {s['cost']:>9,.0f} SEK")
            print(f"      target exit {s['tp']:.2f} (+{TP_PCT*100:.2f}%)   "
                  f"stop {s['sl']:.2f} ({SL_PCT*100:+.2f}%)   "
                  f"exit by {s['exit_by']}")

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
        print(f"\n--- NEW SIGNALS ({len(pending)} cleared but not taken, {len(skipped_signals)} skipped) ---")
        for s in pending:
            tag_str = format_signal_tag(s["info"])
            i = s["info"]
            print(f"  NOT TAKEN {tag_str:<10} {s['ticker']:<18}  last {s['last'] if s['last'] is not None else '?'}   "
                  f"({s.get('not_taken', 'awaiting approval')})   "
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
                  f"exit by {pos['target_exit_date']}   "
                  f"[{ec.sector_map().get(tk) or 'sector ?'}]")

        # Sector concentration (2026-08-31). Informational only — nothing here
        # blocks or resizes an entry. Added after a session where four of the
        # day's six exits were property names that all stopped out together:
        # the engines had no sector data at all, so a correlated book looked
        # exactly like six unrelated positions right up until they moved as one.
        conc = ec.sector_concentration(
            list(state["positions"]),
            {tk: p_["cost_sek"] for tk, p_ in state["positions"].items()})
        invested = sum(g["cost"] for g in conc) or 1.0
        print(f"\n  concentration:")
        for g in conc:
            bar = "#" * int(round(g["cost"] / invested * 20))
            print(f"    {g['sector']:<34} {g['n']:>2} pos  {g['cost']:>9,.0f} SEK  "
                  f"{g['cost']/invested*100:>5.1f}%  {bar}")

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

    # sig-10 / x-4: the names that PASSED signal_check on this run, published as
    # an explicit contract for live_cross. Cross previously keyed off
    # `last_universe ∪ last_watchlist` — the whole radar, ~400 names, which is
    # everything this engine can evaluate rather than anything it likes. That
    # made Cross's "Signals half" a no-op filter. This is the real candidate set:
    # DoD <= DOD_THRESHOLD, not at the 5-day high, not Friday, ret_20d > CRASH_GUARD.
    #
    # `gate` matters as much as the list. Under ENTRY_AT_CLOSE an intraday run
    # still evaluates signal_check against a live mid-session quote, so an
    # intraday hit is a provisional read that can evaporate by 17:30. Only the
    # close-confirmed pass is a real signal, and Cross checks this field rather
    # than trusting the list's mere presence.
    #
    # A user force-entry (state/live_force_entry.json) flips `ok = True` for a
    # name signal_check just REJECTED and appends it to `candidates` like a real
    # hit — and the panel that feeds that button is populated exclusively from
    # names that FAILED signal_check. Publishing those here would have handed
    # Cross a "passed sig-10" row it re-checks nothing of (compute_candidates
    # never re-tests dod / at_5d_high / ret_20d), and Cross would auto-buy ~70k
    # on a Signals half that never fired — the exact defective-gate problem the
    # x-1..x-3 record was thrown away for. The override is the user's business,
    # not a signal, so it is excluded and counted.
    published_hits = [c for c in candidates if not (c["info"] or {}).get("forced")]
    state["last_signal_hits"] = [
        {"ticker":  c["t"]["name"],
         "insref":  c["t"]["insref"],
         "last":    c["live_close"],
         "tier":    "mid/small" if c["t"].get("from_watchlist") else "large cap",
         "dod":     c["info"].get("dod"),
         "ret_20d": c["info"].get("ret_20d"),
         "forced":  False,
         "info":    c["info"]}
        for c in published_hits
    ]
    state["last_signal_hits_meta"] = {
        "date": today.isoformat(),
        "ts":   now_local_ts,
        "gate": "close-confirmed" if close_entry_pass else "intraday",
        "n":    len(published_hits),
        # Named so a day where Cross saw fewer names than the log's candidate
        # count is explainable rather than mysterious.
        "forced_excluded": len(candidates) - len(published_hits),
    }
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
    for s in auto_entries:
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
    combined = ([{**s, "_action": "filled"}  for s in filled_now + auto_entries] +
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

    # sig-9: stamp the close-confirmed pass so it runs exactly once per day.
    # Written at the very end, after entries and limit fills, so a crash midway
    # leaves the date unset and the next post-close tick retries the whole pass
    # rather than silently skipping the day's entries.
    if close_entry_pass:
        state["last_entry_scan_date"] = today.isoformat()
        gate = {"open": True, "reason": "", "date": today.isoformat(),
                "checked": now_local_ts, "entries": len(auto_entries)}
    else:
        gate = {"open": False,
                "reason": "intraday run — entries fire after the close",
                "date": today.isoformat(), "checked": now_local_ts}
    state["entry_gate"] = gate
    # Same contract live_patterns / live_cross publish (last_entry_gate): the
    # reason why nothing was entered. Only `entry_gate` used to be written, and no
    # page reads that key for this wallet — so on the 1M book "the feed never
    # settled", "already scanned today", "before the close" and "every candidate
    # was blocked" all rendered identically to "no signals today". A gate that
    # cannot be seen is the failure shape that hid the 6-second close-gate bug
    # for two months.
    # No last_skipped here, deliberately: web/live_signals.html has no panel
    # reading it, and the not-ready early return above runs on every tick after
    # the day's scan — so a list written here lived one tick before being
    # cleared. Every skip is already logged as a "skipped" event in
    # recent_signals. Publish the list together with the page that reads it.
    state["last_entry_gate"] = gate
    # A close-confirmed pass settles the day's entry question, so a calendar
    # refresh owed by today's EOD sweep is paid here, after the entries. A cache
    # up to a day old loses nothing for a ±7-day window.
    pay_refresh = take_event_cal_refresh(state, today, close_entry_pass)

    save_state(state)
    # Wallet is on disk — now it is safe to announce. Early-return paths (and
    # the EOD sweep, which sells before ever reaching here) are covered by the
    # atexit hook notify registers on the first queued event.
    notify.flush()
    if pay_refresh:
        refresh_event_calendar()


if __name__ == "__main__":
    # main() was called bare. Any exception escaping it killed the process
    # BEFORE the two chained engines below ever ran — and the only LaunchAgent
    # on this machine is this one, so Patterns and Cross exist only as these
    # calls. A fault in Signals (ec.load_state deliberately re-raises on a
    # corrupt live_portfolio.json) therefore left two perfectly healthy books
    # unmanaged — no TP, no SL, no force-sell — on that tick and on every tick
    # after, since a deterministic fault recurs every 180s. Same treatment its
    # two dependents already had.
    try:
        main()
    except Exception:
        import traceback
        print("\n[live_signals crash]")
        traceback.print_exc()
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
    # Breakout engine (2026-10-26) runs after Cross. It reads no other wallet's
    # positions — exempt from the cross-wallet dedupe both ways, see
    # engine_common.WALLET_FILES — only Signals' last_ohlc_capture_date as its
    # feed-settled proof, which main() above has already stamped by now.
    try:
        import live_breakout
        live_breakout.main()
    except Exception as e:
        import traceback
        print("\n[live_breakout crash]")
        traceback.print_exc()
