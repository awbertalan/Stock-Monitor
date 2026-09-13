"""
Live Cross — pattern-confirmed Signals engine. Launched 2026-06-13, rebuilt
2026-09-03 (x-4) around a different question.

THE QUESTION (x-4): does requiring a fresh candlestick fire improve a sig-10
mean-reversion setup? Cross takes the SUBSET of Signals' own entry candidates
that Patterns is simultaneously firing on, at sig-10 params, and its record is
read directly against the Signals wallet over the same period.

WHY THE REBUILD. x-1..x-3 asked "what does Signals ∩ Patterns yield?" and the
answer turned out to be an artefact:

  · The Signals half was not a filter. Cross keyed off `last_universe ∪
    last_watchlist` — every name Signals can evaluate (~400), not the handful
    it wants. Effectively no Signals condition at all.
  · The Patterns half was inverted. live_patterns applies CONFLUENCE_REQUIRED
    inside its ENTRY loop, downstream of where it builds `pattern_fires` and
    publishes `recent_pattern_fires`. Cross read the raw published list, so it
    never inherited the gate: 21 of its 23 scored trades were solo-pattern
    fires — precisely the ones Patterns itself refuses to trade. Cross was
    trading Patterns' rejects and calling it confluence.
  · The 2026-08-18 pause reason was a window mismatch. It cited Tweezer Bottom
    at 69% WR (n=13) in Patterns against 25% (n=8) here; the Patterns sample is
    entirely 2026-05-27..06-10 under pat-1 and Patterns traded ZERO Tweezer
    Bottoms in August. Over the window Cross was actually alive (Aug 1-18),
    Patterns ran 36% WR / -3,136 against Cross's 43% / -6,674. Patterns' good
    August (78% WR, +16,959) is all Aug 19-31, after Cross was already paused.

  So the -6,674 is not evidence against confluence — nothing here ever tested
  confluence. n=23 at 43% vs a 50% break-even is inside noise either way.

DESIGN
  · No new pattern detection — `recent_pattern_fires` from the Patterns engine
    stays the single source of truth. But we now apply the confluence gate that
    Patterns applies to itself (CONFLUENCE_MIN_COUNT), read off that module so
    it cannot drift, and the list only counts if Patterns published it TODAY
    (fresh_pattern_fires). Caveat, not yet fixed: live_patterns detects on the
    last COMPLETED bar and never re-scans after the close, so today's fire
    describes yesterday's bar while the Signals half is today's confirmed
    close. The two halves are one bar apart by construction.
  · Signals half is `last_signal_hits` — names that PASSED signal_check, published
    by live_signals for this purpose. Not the radar. An intraday set still
    renders (marked provisional, always blocked) so the page shows the day
    taking shape; only the close-confirmed set can enter.
  · Auto-entry (2026-09-03). Cross is an A/B arm; hand-picking which candidates
    to approve would make the record a measure of the user's judgment rather
    than of the gate. The approval flow is gone.
  · Close-confirmed entry, matching sig-9/sig-10. Both arms must fill at the
    same confirmed close or a P&L gap is just execution timing — which this
    project's own archive work put at ~45% of the edge.
  · CROSS_WALLET_DEDUPE = False, deliberately. See the constant.

Strategy params (x-4 — mirrors sig-10, NOT pat-4)
  Entry:   sig-10 signal hit AND a multi-pattern fire Patterns published on
           today's own scan, auto-entered at the confirmed close
  Filters: not Friday · ret_20d > -15% · calendar event window · stem-dedup vs
           own book · SL cooldown 1 trading day · 5%-of-median-volume liquidity cap
  Exits:   TP +3.0%, SL -1.0%, max 7-day hold (limit-fill capped via
           apply_exit_checks reused from live_signals)
  Sizing:  target 70k, cap 85k, floor 35k against a 500k SEK wallet

Run lifecycle
  Called from live_signals.__main__ after live_patterns.main() finishes,
  inside the same 3-min LaunchAgent cadence. We use the same load_state /
  save_state / apply_exit_checks helpers as live_signals so cooldown,
  limit-fill capping, conviction sizing all behave identically.
"""

import csv
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

import env_loader                                    # noqa: E402
env_loader.load_env()
import engine_common as ec                           # noqa: E402
import live_signals as L                             # reuse helpers          # noqa: E402
import live_patterns as P                            # confluence gate, read-only  # noqa: E402
import version as V                                  # noqa: E402
import notify                                        # noqa: E402
import calendar_cache                                # noqa: E402

ROOT       = os.path.dirname(os.path.abspath(__file__))
STATE_DIR  = os.path.join(ROOT, "state")
LOGS_DIR   = os.path.join(ROOT, "logs")

STATE_PATH       = os.path.join(STATE_DIR, "live_cross_portfolio.json")
LOG_PATH         = os.path.join(LOGS_DIR,  "live_cross.log")
FORCE_SELL_PATH  = os.path.join(STATE_DIR, "live_cross_force_sell.json")

# Upstream engines we read from
SIGNALS_STATE_PATH  = os.path.join(STATE_DIR, "live_portfolio.json")
PATTERNS_STATE_PATH = os.path.join(STATE_DIR, "live_patterns_portfolio.json")

WALLET_START  = 500_000.0
POS_MIN       =  35_000.0
TARGET_SIZE   =  70_000.0
CAP_SIZE      =  85_000.0

# x-4 mirrors sig-10, NOT pat-4. Cross's record is read against the Signals
# wallet, so its exit parameters have to be Signals'. Until 2026-08-24 all three
# engines shared +2/-2 and the docstring could say "held identical to Signals +
# Patterns"; that stopped being true when Signals reverted its stop, and Cross
# sat on the stale pair for ten days. Break-even WR here is 25.0%, not 50.0%.
TP_PCT        =  0.030   # +3.0% — sig-10
SL_PCT        = -0.010   # -1.0% — sig-10
MAX_HOLD_DAYS = 7
COOLDOWN_DAYS = 1   # SL-exit re-entry block (reduced from 7 → 1 on 2026-06-13: same-day block only, next trading day allowed)
# Not applied in this file, which is deliberate rather than an oversight:
# signal_check already requires ret_20d > CRASH_GUARD (live_signals:443), so
# every row in `last_signal_hits` has cleared it before Cross ever sees the
# name. Re-testing here would only let the two values drift apart. Kept as the
# documented value behind the docstring's "ret_20d > -15%" filter line.
CRASH_GUARD   = -0.15
LATE_ENTRY_CUTOFF_MIN = 16 * 60 + 30   # 16:30 local — no new entries after
MARKET_OPEN_MIN       = 9 * 60         # 09:00 local — mirrors live_signals.is_market_open

# A pattern fire counts as Cross's confluence half only if Patterns published it
# TODAY. That is the same hard date contract signals_hits applies to the Signals
# half, and it is here for the same reason; the rolling-hours window it replaced
# is gone. See fresh_pattern_fires for what that window let through.

# OFF, and it has to stay off for x-4 to mean anything. Cross is an A/B arm over
# the Signals wallet: its whole question is what happens to a sig-10 setup when a
# pattern also fires, which requires taking names Signals is taking at the same
# time. Dedupe would block exactly the overlap being measured and starve the
# engine to near-zero entries — the same collision that makes a strict-confluence
# Cross unrunnable (Patterns runs one step earlier in the chain and grabs the name
# first). The cost is real and accepted: the aggregate book can hold the same
# company twice, so read concentration across all three wallets, not per-wallet.
CROSS_WALLET_DEDUPE = False

# Cross applies the confluence gate live_patterns applies to ITSELF, read off
# that module so the two cannot drift. This is the x-3 defect: live_patterns
# gates on confluence inside its entry loop, downstream of where it publishes
# `recent_pattern_fires`, so every Cross version until now consumed the raw
# list and traded the solo fires Patterns had just rejected — 21 of 23 scored
# trades. Setting CONFLUENCE_REQUIRED = False in live_patterns relaxes both
# engines together, which is the correct coupling.
CONFLUENCE_REQUIRED  = P.CONFLUENCE_REQUIRED
CONFLUENCE_MIN_COUNT = P.CONFLUENCE_MIN_COUNT

# Entries resumed 2026-09-03 (x-4) against a rebuilt gate. See the module
# docstring for why the x-2 pause reason did not survive checking. Set to True
# to pause again; exits, candidates and reporting keep running either way.
ENTRIES_PAUSED = False
# Fill BOTH in at the moment of the flip. They are only read while ENTRIES_PAUSED
# is True (state["paused_since"]/["paused_reason"], the per-candidate skip reason
# and last_entry_gate.reason all None-out otherwise), so a stale payload here is
# invisible until a pause makes it the story the page tells. The x-2 text that sat
# here until 2026-09-05 was exactly that: the Tweezer Bottom comparison the module
# docstring above retracts as a disjoint-window artefact. Do not reuse it.
ENTRIES_PAUSED_SINCE  = "2026-09-03"   # x-4 resume; overwrite with the pause date
ENTRIES_PAUSED_REASON = ("Entries paused — ENTRIES_PAUSED_REASON was not updated "
                         "alongside the flag. Exits, candidates and reporting still run.")

# Auto-entry (2026-09-03), replacing user-approves-every-entry. Cross is a
# controlled arm; a human choosing which candidates to approve makes the record
# a measure of that choosing. state/live_cross_approved.json is no longer read.
AUTO_ENTRY = True

# Fill at the confirmed close, matching sig-9/sig-10. An intraday fill at ask
# against a Signals fill at the close would confound the comparison with
# execution timing, which the minute archive puts at ~45% of the edge.
ENTRY_AT_CLOSE = True

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


def fresh_pattern_fires(patterns_state, today):
    """(fires, why) — the pattern fires Patterns published on ITS OWN scan today.

    `fires` is {ticker: {'patterns': [...], 'ts': iso}}, empty whenever the
    published list cannot be trusted as today's; `why` says which. Same hard
    date contract as signals_hits, and load-bearing for the same reason.

    What the old 30-hour rolling window let through: live_patterns returns
    BEFORE its pattern scan on any tick where the market is closed
    (live_patterns:324) or the clock is past its 16:30 cutoff
    (live_patterns:374), and only the scan writes `recent_pattern_fires`. So at
    17:30 — the only moment Cross's close-confirmed pass can enter — the list is
    at best from the pre-16:30 tick and, on any day the Mac slept through market
    hours (pmset wakes it at 17:48), from an earlier DAY. 30 hours is wider than
    the gap between two daily scans, so the previous session's fire list passed
    as today's confluence and Cross bought a today signal against it, logging
    nothing but `fresh_fires=2`.

    A fire with no `ts` is stale too. It used to count as fresh "so an engine
    that hasn't yet timestamped its fires doesn't go silent" — but an unstamped
    fire cannot be PROVEN to be today's, and a confluence half that is silently
    a day old is worse than one that is honestly missing.

    An empty published list is not a failure: Patterns scanned and nothing
    fired. That returns no candidates and no complaint.
    """
    fires = patterns_state.get("recent_pattern_fires")
    if fires is None:
        return {}, "patterns has not published recent_pattern_fires yet"
    fresh = {}
    seen_dates = set()
    for f in fires:
        tk = f.get("ticker")
        if not tk:
            continue
        ts = _parse_ts(f.get("ts"))
        # `ts` is local naive from live_patterns. If a tz-aware one ever
        # arrives, convert it to local before taking the date — comparing an
        # aware UTC stamp's date against a local trade date is how a 17:30 run
        # ends up reading as the previous day.
        if ts is not None and ts.tzinfo is not None:
            ts = ts.astimezone().replace(tzinfo=None)
        seen_dates.add(ts.date().isoformat() if ts is not None else "undated")
        if ts is None or ts.date() != today:
            continue
        fresh[tk] = {"patterns": f.get("patterns") or [], "ts": f.get("ts")}
    if not fresh and seen_dates:
        return {}, ("patterns has not scanned today — its published fires are from "
                    + ", ".join(sorted(seen_dates)))
    return fresh, ""


def signals_hits(signals_state, today):
    """(hits, why) — the names that PASSED signal_check on Signals' last run.

    `hits` is {ticker: {'dod':…, 'ret_20d':…, 'tier':…, 'last':…, 'insref':…}},
    empty whenever the published set cannot be trusted; `why` says which.

    Two guards, both load-bearing:

      · The set must be from TODAY. `last_signal_hits` is a snapshot, and a
        stale one is worse than none — it would let Cross enter on yesterday's
        down-day against today's price, which is the trap that the STALE exit
        guard was added for on the exit side.
      · The set must come from Signals' CLOSE-CONFIRMED pass. Under
        ENTRY_AT_CLOSE an intraday run still evaluates signal_check against a
        mid-session quote, so an intraday hit is provisional and routinely
        evaporates by 17:30. Cross fills at the close, so it wants only the
        pass Signals itself acts on.

    Returns ({}, reason) rather than raising if live_signals predates the
    `last_signal_hits` contract — Cross then simply reports no candidates.
    """
    meta = signals_state.get("last_signal_hits_meta") or {}
    rows = signals_state.get("last_signal_hits")
    if rows is None:
        return {}, "signals has not published last_signal_hits yet"
    if meta.get("date") != today.isoformat():
        return {}, f"signals hits are from {meta.get('date') or '?'}, not today"
    # An intraday set is returned, but every row is marked provisional so the
    # entry path rejects it while the page can still show the day building. The
    # alternative — returning {} until 17:30 — leaves this page empty through
    # every market hour, which reads as "nothing is happening" rather than
    # "nothing is confirmed yet".
    provisional = meta.get("gate") != "close-confirmed"
    hits = {}
    for r in rows:
        tk = r.get("ticker")
        if tk:
            hits[tk] = dict(r, provisional=provisional)
    if provisional:
        return hits, f"signals has not run its close-confirmed pass yet (gate={meta.get('gate') or '?'})"
    return hits, ""


def close_confirmed_entry_ready(state, signals_state, today):
    """(ready, reason) for today's post-close entry pass — Cross's own copy.

    Deliberately NOT live_signals.close_confirmed_entry_ready: that one reads
    `last_ohlc_capture_date` off the state it is handed, and only the Signals
    engine captures OHLC. Cross tracks its own `last_entry_scan_date` (so its
    pass fires once per day independently) but borrows the feed-settled proof
    from the Signals state, which is the file that actually stamps it.
    """
    if not ENTRY_AT_CLOSE:
        return False, "close-confirmed entry disabled"
    iso = today.isoformat()
    if state.get("last_entry_scan_date") == iso:
        return False, "already scanned after today's close"
    now_local = datetime.now()
    if now_local.weekday() >= 5:
        return False, "weekend"
    mins_now = now_local.hour * 60 + now_local.minute
    if mins_now < L.MARKET_CLOSE[0] * 60 + L.MARKET_CLOSE[1]:
        return False, "before the 17:30 close"
    if signals_state.get("last_ohlc_capture_date") != iso:
        return False, "feed has not settled past today's close yet"
    return True, ""


# ── Universe + filters ───────────────────────────────────────────────────────

def is_friday_today():
    return ec.is_friday()


def past_late_entry_cutoff():
    """Kept for the log line only — NOT an entry gate under ENTRY_AT_CLOSE.

    Same call as live_signals:104. The close-confirmed pass by definition runs
    after 17:30, so wiring this into the entry path would reject every single
    candidate, every day, silently. It stays as the honest answer to "would an
    intraday run have been too late?" and nothing more.
    """
    return ec.past_cutoff(LATE_ENTRY_CUTOFF_MIN)


def exits_can_book(now_local=None):
    """(ok, why) — may this tick book exits at all?

    Cross is the only engine that reaches apply_exit_checks before the open:
    live_signals returns on the closed-market path before its exit pass and
    live_patterns returns outright on `not is_market_open()`, while Cross
    deliberately falls through so a post-close tick can still manage the book.

    Before 09:00 `L.get_live()` answers with the PREVIOUS session's last trade —
    ok=True and no `source` marker, so neither the None guard nor the stale
    guard inside apply_exit_checks stops it. That is the same "book exits
    against yesterday's prices" hazard live_signals:726 already names: the fill
    is a price that is no longer on offer anywhere.

    What this guard does NOT do, contrary to what this comment claimed when it
    was written: it does not lengthen a hold. The TIMEOUT branch is a pure
    calendar test (`today >= target_date`, live_signals:934), equally true at
    06:30 and at 09:03 on the target date, so the exit still books on the same
    day — only the fill moves, from yesterday's close to the first live print of
    the session, which is an overnight gap either way. Nor does it suppress a
    TP or an SL: pre-open those are price tests against the very close the 17:33
    tick already evaluated, so a pre-open tick could never book one the previous
    evening had not. The guard is exactly "do not fill against a pre-open
    quote", and nothing more. A post-close tick still books normally, which is
    the window this fall-through exists for.
    """
    if now_local is None:
        now_local = datetime.now()
    mins = now_local.hour * 60 + now_local.minute
    if now_local.weekday() < 5 and mins < MARKET_OPEN_MIN:
        return False, (f"pre-open tick ({now_local.strftime('%H:%M')}) — the feed still "
                       f"carries the previous session's close")
    return True, ""


# ── Candidate computation ────────────────────────────────────────────────────

def compute_candidates(signals_state, patterns_state, state, today, fires):
    """Build the list of cross-radar candidates.

    A ticker is a candidate when:
      · It PASSED Signals' signal_check on today's close-confirmed pass
        (`last_signal_hits`) — not merely somewhere in Signals' radar, which is
        what x-1..x-3 checked and which filtered nothing
      · AND Patterns published a recent_pattern_fires entry on it on today's
        own scan (`fires`, from fresh_pattern_fires)
      · AND we are not already long it on the Cross engine

    For each candidate we record any blocking reasons (confluence, calendar
    window, SL cooldown, Friday, liquidity, stem dup) so the page can show why
    a name Cross wanted did not trade. Only gates whose answer cannot change
    later in the run belong here — see the cash floor below. Blocked candidates
    still appear — an empty list must never be the only trace of a gate that
    rejected everything.
    """
    hits, hits_why = signals_hits(signals_state, today)

    # Build cooldown/recent-exit lookup for Cross's own trades
    recent_exits = ec.build_recent_exits(state)

    held_stems = {L.company_stem(tk) for tk in state["positions"]}
    # Both upstream states are already in hand, so pass them in rather than
    # re-reading the same two files off disk.
    foreign_stems = (ec.foreign_held_stems(
        ec.sibling_wallets("cross"), L.company_stem,
        states={"Signals": signals_state, "Patterns": patterns_state})
        if CROSS_WALLET_DEDUPE else {})

    # Last known price per ticker, for the liquidity cap below. compute_candidates
    # runs before this engine fetches its own quotes, so it borrows the upstream
    # engines' snapshots; the entry loop re-checks against a live quote.
    ref_price = {}
    for src_state in (patterns_state, signals_state):
        for tk, q in (src_state.get("last_quotes") or {}).items():
            if isinstance(q, dict) and q.get("last"):
                ref_price.setdefault(tk, q["last"])

    # Build insref lookup from the upstream engines' ticker_paths and stock_names.csv
    insref_map = _insref_map(signals_state, patterns_state)

    # Tier comes off the signal hit itself now; the radar sets are only a
    # fallback for a hit row written before `tier` was added.
    sig_uni = set(signals_state.get("last_universe") or [])
    sig_wl  = set(signals_state.get("last_watchlist") or [])

    # Tradable-folder paths for UI click-through
    ticker_paths = {}
    for src in (signals_state.get("ticker_paths") or {}, patterns_state.get("ticker_paths") or {}):
        for tk, rp in src.items():
            ticker_paths.setdefault(tk, rp)

    events_cache = calendar_cache.load_cache()

    candidates = []
    for tk in sorted(set(hits) & set(fires)):
        if tk in state["positions"]:
            continue   # already long it on Cross
        hit = hits[tk]
        insref = hit.get("insref") or insref_map.get(tk)
        # `patterns` is Patterns' entry-eligible list — ENTRY_DISABLED_PATTERNS
        # (Hammer) are already stripped upstream, so a Hammer-only fire arrives
        # here as an empty list rather than as a tradeable signal.
        pats = fires[tk]["patterns"]
        skip = []
        if ENTRIES_PAUSED:
            skip.append(f"entries paused since {ENTRIES_PAUSED_SINCE}")
        # Provisional: Signals liked this name on a mid-session quote, which is
        # not the close it will actually be judged on. Blocked, so it can never
        # enter, but visible so the page shows the day taking shape.
        if hit.get("provisional"):
            skip.append("provisional — Signals confirms at the close")
        # Confluence — the gate live_patterns applies to itself (x-4). Until now
        # Cross read the published fire list without it and traded the rejects.
        if not pats:
            skip.append("only entry-disabled pattern(s) fired")
        elif CONFLUENCE_REQUIRED and len(pats) < CONFLUENCE_MIN_COUNT:
            skip.append(f"solo {pats[0]} (confluence requires {CONFLUENCE_MIN_COUNT}+)")
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
        # Stem dedup — own book, then the other two wallets
        stem = L.company_stem(tk)
        if stem in held_stems:
            skip.append(f"duplicate stem '{stem}'")
        elif stem in foreign_stems:
            skip.append(f"held in another wallet ({foreign_stems[stem]})")
        # Liquidity — can this name absorb at least the position floor?
        px = hit.get("last") or ref_price.get(tk)
        if insref and px:
            _, liq_skip = ec.apply_liquidity_cap(
                ec.liquidity_verdict(insref, px), CAP_SIZE, POS_MIN)
            if liq_skip:
                skip.append(liq_skip)
        # Friday. Redundant with signal_check (which fails on a Friday, so no hit
        # can reach here on one) but kept as the explicit statement of the rule.
        # The 16:30 cutoff is deliberately NOT applied — see past_late_entry_cutoff.
        if is_friday_today():
            skip.append("no Friday entries")
        # Cash floor is deliberately NOT tested here. compute_candidates runs
        # BEFORE apply_exit_checks, so `state["cash"]` is the PRE-exit balance;
        # baking a "low cash" flag into `blocked` made the entry loop `continue`
        # straight past the names the day's TP exits had just paid for, and
        # `last_entry_scan_date` is stamped either way so the day was gone. The
        # loop re-checks cash at fill time against the post-exit balance, which
        # is what live_signals:1249 does. The page also stopped showing "low
        # cash 20000" next to a 380k cash tile.

        candidates.append({
            "ticker":          tk,
            "insref":          insref,
            "patterns":        pats,
            "pattern_fire_ts": fires[tk]["ts"],
            "tier":            hit.get("tier")
                               or ("large cap" if tk in sig_uni else
                                   ("mid/small" if tk in sig_wl else "?")),
            "signal_confirmed":  True,   # passed sig-10 signal_check today
            # The sig-10 numbers behind the Signals half, so the page can show
            # WHY Signals liked the name rather than just asserting that it did.
            "dod":             hit.get("dod"),
            "ret_20d":         hit.get("ret_20d"),
            "signal_last":     hit.get("last"),
            "ticker_path":     ticker_paths.get(tk),
            "skip_reasons":    skip,
            "blocked":         bool(skip),
        })
    # Strongest signal first — most-negative DoD, matching how live_signals ranks
    # its own candidates, so that when the wallet runs out of cash both engines
    # have spent it on the same names rather than on different halves of the list.
    candidates.sort(key=lambda c: (c["blocked"], c.get("dod") if c.get("dod") is not None else 0.0))
    return candidates, hits_why


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

    # ── 1. Load upstream engine state ────────────────────────────────────────
    # Read before the market gate: the close-confirmed check needs Signals'
    # last_ohlc_capture_date to know whether the feed has settled.
    signals_state  = _read_json(SIGNALS_STATE_PATH,  {})
    patterns_state = _read_json(PATTERNS_STATE_PATH, {})

    # Resolved once, here, because the gate dicts written on the early-return
    # path below have to be able to report a down pattern half too — a half that
    # is stale is exactly the state that makes this engine look idle.
    fires, fires_why = fresh_pattern_fires(patterns_state, today)

    # x-4: entries are close-confirmed, so a post-close tick falls THROUGH into
    # the scan below rather than returning, exactly as live_signals does. Exits
    # run on every tick either way — an intraday run still manages the book, it
    # just cannot open anything.
    if L.is_market_open():
        close_entry_pass, gate_why = False, "intraday run — entries fire after the close"
    else:
        close_entry_pass, gate_why = close_confirmed_entry_ready(state, signals_state, today)
        if not close_entry_pass:
            # Nothing to enter, but exits still have to be applied on the
            # post-close tick, so we do NOT return here. Only a run with no
            # open book has nothing left to do.
            if not state["positions"]:
                # Write the SAME gate dict to both keys and clear last_skipped.
                # The page reads `last_entry_gate || entry_gate`, so updating
                # only the fallback republishes the previous trading day's
                # reason under a fresh last_run — a Saturday tick rendering
                # Friday's "no Friday entries". Stale behind a fresh clock is
                # the exact failure shape this project keeps hitting.
                gate = {"open": False, "reason": gate_why, "signals_half": "ok",
                        "patterns_half": fires_why or "ok",
                        "date": today.isoformat(), "checked": now_local_ts,
                        "entries": 0}
                state["entry_gate"] = gate
                state["last_entry_gate"] = gate
                state["last_skipped"] = []
                state["pending_candidates"] = []
                # exit_pass has to belong to the run that stamped last_run on
                # THIS path too. It is written unconditionally at the end of a
                # full run; skipping it here let a Friday close's "closed: 1"
                # ride out the weekend under every fresh Saturday timestamp —
                # the same stale-behind-a-fresh-clock trap the gate above is
                # written to both keys to avoid. force_sell_blocked likewise:
                # with an empty book there is nothing left queued.
                state["exit_pass"] = {"ran": False, "reason": "no open positions",
                                      "closed": 0, "checked": now_local_ts}
                state["force_sell_blocked"] = []
                save_state(state)   # stamps last_run itself
                out(f" Market closed, no open positions, no entry pass: {gate_why}.")
                log.close()
                return
            out(f" Market closed — exits only, no entry pass: {gate_why}.")
        else:
            out(" CLOSE-CONFIRMED ENTRY PASS — feed settled past today's close.")

    # ── 2. Compute candidate set + persist for UI ───────────────────────────
    candidates, hits_why = compute_candidates(
        signals_state, patterns_state, state, today, fires)
    state["pending_candidates"] = candidates
    state["entries_paused"]     = ENTRIES_PAUSED
    state["paused_since"]       = ENTRIES_PAUSED_SINCE  if ENTRIES_PAUSED else None
    state["paused_reason"]      = ENTRIES_PAUSED_REASON if ENTRIES_PAUSED else None
    sig_hits, _ = signals_hits(signals_state, today)
    out(f" Cross candidates: {len(candidates)}  "
        f"(signals_hits={len(sig_hits)}, fires_today={len(fires)})")
    if hits_why:
        # The Signals half being empty is the single most likely reason this
        # engine looks idle, and it is invisible from the candidate count alone.
        out(f"   signals half unavailable: {hits_why}")
    if fires_why:
        # Same for the pattern half. The old log line said only `fresh_fires=2`
        # with no age and no date, so a two-bar-old confluence read exactly like
        # a current one.
        out(f"   patterns half unavailable: {fires_why}")
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
    exits_ok, exits_why = exits_can_book(now_local)
    closed_now = []
    if not exits_ok:
        # Deferred, not skipped: the next tick from the open onward books
        # whatever was due. See exits_can_book for what a pre-open fill is worth.
        out(f" Exits deferred — {exits_why}.")
        # A queued "Sell now" is user intent, and this branch skips
        # apply_exit_checks — the ONLY writer of force_sell_blocked
        # (live_signals:975) and of the per-position DEFERRED log line it was
        # given precisely so a swallowed sell could not stay invisible
        # (live_signals:880-893). So the deferral has to publish that record
        # itself: named in the log, persisted with a reason, and the queue file
        # left untouched so the intent survives to the first tick after the
        # open. See the standing rule on silent gates.
        queued = sorted(tk for tk in force_sells if tk in state["positions"])
        for tk in queued:
            out(f"   MANUAL sell for {tk} DEFERRED — {exits_why}; stays queued "
                f"for the first tick after the open")
        state["force_sell_blocked"] = [{"ticker": tk, "reason": exits_why,
                                        "source": "pre-open", "ts": now_local_ts}
                                       for tk in queued]
    else:
        closed_now = L.apply_exit_checks(state, today, now_local_ts, live, force_sells,
                                         engine="cross")
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

    # ── 5. New entries — auto, over every unblocked candidate (x-4) ─────────
    if ENTRIES_PAUSED:
        out(" ENTRIES PAUSED (ENTRIES_PAUSED=True) — exits and reporting still run.")
    if is_friday_today():
        out(" No entries on Friday (weekend-risk rule).")

    new_entries = []
    new_skipped = []
    entered_stems = {L.company_stem(tk) for tk in state["positions"]}

    # Every candidate that carries a skip reason is reported, not silently
    # dropped: a run where the gate rejected everything has to look different
    # from a run where nothing fired.
    for c in candidates:
        if c["blocked"]:
            new_skipped.append({"ticker": c["ticker"],
                                "reason": " · ".join(c["skip_reasons"])})

    if close_entry_pass and fires_why:
        out(f" No entries — the pattern half cannot be confirmed for today: {fires_why}.")
    # `not fires_why` is redundant with the empty candidate list it implies, and
    # stated anyway: Cross's whole question is a signal AND a pattern fire, so an
    # unconfirmable pattern half must read as a closed gate, never as a quiet day.
    if close_entry_pass and not ENTRIES_PAUSED and not is_friday_today() and not fires_why:
        for cand in candidates:
            if cand["blocked"]:
                continue
            tk = cand["ticker"]
            lq = live.get(tk)
            if not lq or not lq.get("ok") or lq.get("last") in (None, 0):
                new_skipped.append({"ticker": tk, "reason": "no live quote"})
                continue
            if state["cash"] < POS_MIN:
                new_skipped.append({"ticker": tk, "reason": f"low cash {state['cash']:.0f}"})
                continue
            # Fill at the confirmed close, matching live_signals:1225. After
            # 17:30 the ask is a stale end-of-session quote that can sit well
            # away from where the stock settled; taking it would hand Cross a
            # different (worse) entry than the Signals arm on the same name and
            # make the whole comparison meaningless. Ask is kept only for the
            # intraday path, which under ENTRY_AT_CLOSE never enters.
            entry_price = lq["last"] if close_entry_pass else (lq.get("ask") or lq["last"])
            if not entry_price:
                new_skipped.append({"ticker": tk, "reason": "no live quote"})
                continue
            # Stem dedup WITHIN this run — compute_candidates snapshots held_stems
            # once, before any of today's fills, so without this a pass that
            # entered Electrolux A could enter Electrolux B three lines later.
            stem = L.company_stem(tk)
            if stem in entered_stems:
                new_skipped.append({"ticker": tk, "reason": f"duplicate stem '{stem}' entered this run"})
                continue
            liq = ec.liquidity_verdict(cand["insref"], entry_price, lq)
            cap, liq_skip = ec.apply_liquidity_cap(liq, CAP_SIZE, POS_MIN)
            if liq_skip:
                new_skipped.append({"ticker": tk, "reason": liq_skip})
                continue
            shares, cost = ec.position_size(state["cash"], entry_price, TARGET_SIZE, cap, POS_MIN)
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
                    # The sig-10 numbers this entry rested on, stamped at fill
                    # time. Without them a Cross trade cannot be matched back to
                    # the Signals setup it was supposed to be an arm of.
                    "dod":            cand.get("dod"),
                    "ret_20d":        cand.get("ret_20d"),
                    "entered_at":     now_local_ts,
                },
                "fill_type":        "close" if close_entry_pass else "auto",
                **V.stamp("cross"),
            }
            entered_stems.add(stem)
            new_entries.append({
                "ticker": tk, "pattern": primary_pattern, "all_patterns": cand["patterns"],
                "tier": cand["tier"], "entry": entry_price, "shares": shares, "cost": cost,
                "tp": tp_price, "sl": sl_price, "exit_by": exit_by.isoformat(),
                "dod": cand.get("dod"),
            })
            notify.queue_buy("cross", tk, entry_price, shares, cost=cost,
                             tp=tp_price, sl=sl_price, exit_by=exit_by.isoformat(),
                             pattern="sig-10 + " + " + ".join(cand["patterns"]))

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
            dod_str = f"DoD {e['dod']*100:+.2f}%" if e.get("dod") is not None else "DoD ?"
            out(f"      {dod_str}   target {e['tp']:.2f} (+{TP_PCT*100:.2f}%)  "
                f"stop {e['sl']:.2f} ({SL_PCT*100:+.2f}%)  exit by {e['exit_by']}")
    elif candidates:
        out(f"\n--- NO NEW CROSS ENTRIES ({gate_why or 'every candidate blocked'}) ---")

    if new_skipped:
        out(f"\n--- BLOCKED THIS RUN ({len(new_skipped)}) ---")
        for s in new_skipped[:10]:
            out(f"  skipped {s['ticker']:<22} {s['reason']}")
        if len(new_skipped) > 10:
            out(f"  …+{len(new_skipped) - 10} more")

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

    # Gate decisions persisted for the page (same contract as live_patterns'
    # last_skipped / last_entry_gate). Written here, at the end, so the list
    # always belongs to the run that stamped last_run — a rejection list left
    # over from an earlier tick under a fresh timestamp is the stale-behind-a-
    # fresh-clock trap that hid frozen prices on these pages for weeks.
    state["last_skipped"] = new_skipped
    state["last_entry_gate"] = {
        "open":    bool(close_entry_pass and not ENTRIES_PAUSED and not is_friday_today()
                        and not fires_why),
        "reason":  (f"entries paused since {ENTRIES_PAUSED_SINCE}" if ENTRIES_PAUSED
                    else "no Friday entries" if is_friday_today()
                    # gate_why is empty on a confirmed pass, and a down pattern
                    # half is then the whole reason nothing can enter.
                    else gate_why or fires_why),
        "signals_half": hits_why or "ok",
        "patterns_half": fires_why or "ok",
        "date":    today.isoformat(),
        "checked": now_local_ts,
        "entries": len(new_entries),
    }
    state["entry_gate"] = state["last_entry_gate"]

    # Why the book went unmanaged on this tick, if it did — same
    # stale-behind-a-fresh-clock rule as the gate above: written every run, so
    # it always belongs to the run that stamped last_run.
    state["exit_pass"] = {"ran": exits_ok, "reason": exits_why or "ok",
                          "closed": len(closed_now), "checked": now_local_ts}

    # x-4: stamp the close-confirmed pass so it runs exactly once per day.
    # Written last, after entries, so a crash midway leaves the date unset and
    # the next post-close tick retries the whole pass rather than silently
    # skipping the day (live_signals:1642 does the same for the same reason).
    # A down pattern half does NOT hold the stamp back, even though the pass
    # then enters nothing. Withholding it only buys a retry if the half could
    # still come up today, and after 17:30 it cannot: live_patterns publishes
    # recent_pattern_fires at the very end of its scan (live_patterns:633) and
    # returns before it on the closed-market path (live_patterns:338), past its
    # 16:30 cutoff (live_patterns:406) and all day on a Friday
    # (live_patterns:398). Nothing can republish that list between now and
    # midnight, so an unstamped date just re-runs this whole pass every 3
    # minutes until then — ~130 futile ticks on every Friday evening, plus any
    # day the Mac slept through market hours.
    if close_entry_pass:
        state["last_entry_scan_date"] = today.isoformat()

    save_state(state)
    notify.flush(out=out)     # only after the wallet is written — see lib/notify.py
    log.close()


def _report_open(state, live, out):
    ec.report_open(state, live, out, "  [X]  ")


if __name__ == "__main__":
    main()
