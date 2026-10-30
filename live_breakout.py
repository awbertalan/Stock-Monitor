"""
Live Breakout — 52-week-high breakout engine, held 4-12 weeks. Launched
2026-10-26 (brk-1).

THE QUESTION. The other three engines are short mean-reversion trades (days).
This one asks whether a LONG-hold momentum setup earns its keep next to simply
owning the index: buy the strongest fresh 52-week highs among the most-traded
Swedish stocks and hold them for weeks.

THE BACKTEST (2026-10-26, Instrumenttype/** daily closes 2003-2026, this exact
rule set, 20 slots, 0.15% round trip, fills modelled on what the 2026 minute
archive says an intraday +10% / -15% actually triggers at): ~13.9% CAGR against
8.7% for the OMX Stockholm price index, max drawdown ~-19% against -59%. It is a
paper wallet because of the caveats, not despite them:
  · it beat the index in only 12 of 24 years, and lagged it EVERY year
    2023-2026 (avg trade ~+0.5% since 2023);
  · the big years came from a few speculative runners — Sensys Gatso and
    Fingerprint Cards were ~15 points of 2015 on their own;
  · the archive holds only stocks still listed today, which flatters exactly
    this kind of strategy, and its closes carry no dividends;
  · on the 50 most-traded names alone it LOSES to the index — the edge lives in
    the less-traded end of the top 150.
  Judge the record against the index line on the page, not against zero.

DESIGN
  · Universe: every Equity/SEK folder with >= MIN_HISTORY sessions, ranked by
    60-session median turnover (close x volume, completed sessions only); the
    top UNIVERSE_SIZE trade. Point-in-time, so a speculative name enters the
    universe only while it actually trades heavily.
  · Entry: today's confirmed close is above the highest close of the previous
    250 sessions, AND the name made no such high in the previous 20 sessions —
    a fresh breakout, not day 37 of a run. When more names qualify than slots
    are free, the strongest 12-month momentum (close 21 sessions ago over close
    250 sessions ago, the classic 12-1) goes first.
  · Close-confirmed fill, once per day, gated on the same feed-settled proof
    Cross uses (Signals' last_ohlc_capture_date). No entry cutoff: on a 12-week
    hold the fill is the official close whenever the evening pass runs, and the
    backtest put a whole day's delay at ~0.7pp CAGR.
  · Exits: SL -15% from day one; TP +10% switched on only from the 20th
    session after entry (week 4); TIMEOUT at 60 sessions (12 weeks). A
    position already above +10% when its target switches on is sold at MARKET
    on that first look, not booked at exactly +10% — the shared exit routine's
    limit-fill cap is right for a target that was live all along, and wrong for
    one that had been switched off. Modelling that day as a +10% fill took the
    backtest from ~13% CAGR to ~5%. See arm_targets().
  · Sizing: 20 equal slots — target = equity / SLOTS, floor half a slot.
  · Gates applied: own-book company-stem dedupe (no Volvo A + Volvo B), the
    shared 5%-of-volume liquidity cap, cash, slots. Gates deliberately NOT
    applied, because the backtest had none of them: no Friday rule, no
    earnings/event window, no SL cooldown, no cross-wallet dedupe.
  · CROSS_WALLET_DEDUPE = False, and this wallet is deliberately absent from
    engine_common.WALLET_FILES, so the exemption holds in both directions —
    owner decision 2026-10-26. A 12-week position must not block Signals or
    Patterns from a company for 12 weeks, and neither may they block this one;
    otherwise no wallet trades the rules it was tested on. The cost: the same
    company can sit in two wallets at once.

Run lifecycle
  Called from live_signals.__main__ after live_cross.main(), inside the same
  3-min LaunchAgent cadence. Exits run on every tick with an open book; the
  universe scan and entries run once, on the first post-close tick after the
  feed settles.
"""

import csv
import glob
import math
import os
import statistics
import sys
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

import env_loader                                    # noqa: E402
env_loader.load_env()
import engine_common as ec                           # noqa: E402
import live_signals as L                             # reuse helpers          # noqa: E402
import version as V                                  # noqa: E402
import notify                                        # noqa: E402

ROOT       = os.path.dirname(os.path.abspath(__file__))
STATE_DIR  = os.path.join(ROOT, "state")
LOGS_DIR   = os.path.join(ROOT, "logs")
EQUITY_DIR = os.path.join(ROOT, "Instrumenttype", "Equity", "SEK")
NAMES_CSV  = os.path.join(ROOT, "stock_names.csv")

# OMX Stockholm PI, read as a live quote. Not its _History.csv: the scheduled
# sweep refreshes Equity/SEK only (settings update_filter), so that file is
# whatever a one-off refresh last wrote — on 2026-10-26 an 11:00 print.
INDEX_INSREF = 6486
INDEX_NAME   = "OMX Stockholm PI (price)"

ENGINE_KEY       = "breakout"
STATE_PATH       = os.path.join(STATE_DIR, "live_breakout_portfolio.json")
LOG_PATH         = os.path.join(LOGS_DIR,  "live_breakout.log")
FORCE_SELL_PATH  = os.path.join(STATE_DIR, "live_breakout_force_sell.json")

# Read-only: the feed-settled proof for the close-confirmed pass. Only Signals
# captures the EOD bar, so only its wallet stamps last_ohlc_capture_date.
SIGNALS_STATE_PATH = os.path.join(STATE_DIR, "live_portfolio.json")

WALLET_START   = 1_000_000.0
SLOTS          = 20      # equal weight: each position targets equity / SLOTS
POS_FLOOR_FRAC = 0.5     # skip a fill smaller than half a slot (backtest rule)

UNIVERSE_SIZE   = 150    # top-N by turnover; on the top 50 alone the edge is gone
TURNOVER_WINDOW = 60     # sessions in the median close x volume
MIN_HISTORY     = 252    # completed sessions a name needs to be ranked at all
HIGH_LOOKBACK   = 250    # the 52-week high = max of the previous 250 closes
HIGH_MIN_PRIOR  = 240    # a session needs this many prior closes to count as a high
FRESH_GAP       = 20     # no new 52-week high in the previous 20 sessions
MOM_SKIP, MOM_SPAN = 21, 250   # priority: close[t-21] / close[t-250] - 1
TAIL_ROWS       = 320    # History rows read per name — covers every lookback above

TP_PCT            = 0.10   # +10% — live only from TP_FROM_SESSIONS
TP_FROM_SESSIONS  = 20     # week 4: the target switches on at the 20th session
SL_PCT            = -0.15  # -15% — live from day one
MAX_HOLD_SESSIONS = 60     # 12 weeks

# Stands in for a target that is not live yet while apply_exit_checks runs. It
# never reaches disk: arm_targets() swaps it in and restore_targets() swaps the
# real value back before anything is saved.
TP_PARKED = 1e18

MARKET_OPEN_MIN = 9 * 60   # 09:00 local — mirrors live_signals.is_market_open
# The feed runs ~15 minutes behind (live_signals.log 2026-10-26: the 09:14 probe
# saw no trades yet, the 09:17 one saw SEB A's 09:02 print). With no usable
# probe on record, exits wait until this minute — see exits_can_book().
FEED_FALLBACK_OPEN_MIN = 9 * 60 + 20

# Data guards on the daily scan — see scan_problem().
MAX_STALE_SHARE    = 0.25  # more than this share of names behind = history is down
MIN_QUOTE_COVERAGE = 0.90  # fewer quoted than this = retry the pass ...
MAX_SCAN_ATTEMPTS  = 3     # ... at most this many times, then scan what answered

# Exempt in both directions — see the module docstring.
CROSS_WALLET_DEDUPE = False

# Set to True to pause new entries; exits, the daily scan and reporting keep
# running. Fill BOTH strings in at the moment of the flip — they are only read
# while the flag is True, so a stale reason would surface unnoticed.
ENTRIES_PAUSED = False
ENTRIES_PAUSED_SINCE  = None
ENTRIES_PAUSED_REASON = ("Entries paused — ENTRIES_PAUSED_REASON was not updated "
                         "alongside the flag. Exits, the scan and reporting still run.")

LOG_RETAIN_HOURS     = 24
LOG_ROTATE_MIN_BYTES = 200_000


# ── State persistence ────────────────────────────────────────────────────────

def load_state():
    return ec.load_state(STATE_PATH, {
        "cash":           WALLET_START,
        "wallet_start":   WALLET_START,
        "positions":      {},
        "closed_trades":  [],
        "inception_date": ec.today_local().isoformat(),
        "last_run":       None,
    })


def save_state(state):
    ec.save_state(STATE_PATH, state, in_session=L.is_market_open())


def rotate_log():
    """Trim live_breakout.log to the last 24h — see engine_common.rotate_log."""
    ec.rotate_log(LOG_PATH, " Live breakout run @ ", LOG_RETAIN_HOURS, LOG_ROTATE_MIN_BYTES)


# ── History ──────────────────────────────────────────────────────────────────

def read_history_tail(path, n_rows=TAIL_ROWS):
    """The last `n_rows` (date, close, volume) rows of a _History.csv, oldest
    first.

    Reads from the end of the file instead of parsing all ~8,000 rows: the
    daily scan touches every Equity/SEK folder (~986), and only the last ~300
    sessions matter. All 986 files were checked sorted and duplicate-free on
    2026-10-26; rows that do not parse are skipped, as in
    live_signals.load_history_closes."""
    size = os.path.getsize(path)
    chunk = max(8192, n_rows * 48)
    with open(path, "rb") as f:
        while True:
            start = max(0, size - chunk)
            f.seek(start)
            lines = f.read().decode("utf-8", errors="replace").splitlines()
            if start > 0:
                lines = lines[1:]            # the first line may be cut in half
            if start == 0 or len(lines) > n_rows:
                break
            chunk *= 2
    rows = []
    for ln in lines:
        parts = ln.split(",")
        if len(parts) < 2:
            continue
        # The date conversion sits inside the try too: an `inf` or absurd
        # timestamp raises OverflowError/OSError there, and one bad row in any
        # of ~986 files must cost that row, not the evening's pass.
        try:
            close = float(parts[1])
            vol = float(parts[2]) if len(parts) > 2 and parts[2].strip() else 0.0
            d = datetime.fromtimestamp(int(float(parts[0])) / 1000, tz=timezone.utc).date()
        except (ValueError, OverflowError, OSError):
            continue
        if close > 0 and math.isfinite(close) and math.isfinite(vol):
            rows.append((d, close, vol))
    return rows[-n_rows:]


def load_names():
    """insref -> display name from stock_names.csv (column 1), the name every
    engine keys its positions by and every live page renders."""
    out = {}
    try:
        with open(NAMES_CSV, newline="", encoding="utf-8") as f:
            for row in csv.reader(f):
                if len(row) >= 2:
                    try:
                        out.setdefault(int(row[0]), row[1])
                    except ValueError:
                        pass
    except OSError:
        pass
    return out


def previous_session(today):
    """The last trading day before `today` — the newest row a current
    _History.csv must hold once today's own row is set aside."""
    d = today - timedelta(days=1)
    while not L.is_trading_day(d):
        d -= timedelta(days=1)
    return d


def build_universe(today):
    """(universe, meta). `universe` is the top UNIVERSE_SIZE names by median
    turnover over the last TURNOVER_WINDOW completed sessions, each a dict with
    the History rows the signal needs. Today's row, if the sweep has already
    written one, is dropped here: the scan appends the confirmed close from the
    live quote instead, the same way live_signals builds its closes.

    A name whose History stops before the previous session is left out and
    counted as `stale`. Only app.py's sweep writes these files, and with it down
    the 250-session window would silently skip the missing days — a stock whose
    real high fell in the gap would read as a fresh breakout. scan_problem()
    closes the whole pass when too many names are behind."""
    names = load_names()
    want = previous_session(today)
    ranked, unreadable, short, stale = [], 0, 0, 0
    newest = None
    for folder_path in glob.glob(os.path.join(EQUITY_DIR, "*_*")):
        folder = os.path.basename(folder_path)
        try:
            insref = int(folder.split("_", 1)[0])
        except ValueError:
            continue
        hist = glob.glob(os.path.join(folder_path, "*_History.csv"))
        if not hist:
            continue
        try:
            rows = read_history_tail(hist[0])
        except Exception:                       # noqa: BLE001 — one file, not the pass
            unreadable += 1
            continue
        if rows and rows[-1][0] >= today:
            rows = rows[:-1]
        if len(rows) < MIN_HISTORY:
            short += 1
            continue
        newest = max(newest, rows[-1][0]) if newest else rows[-1][0]
        if rows[-1][0] < want:
            stale += 1
            continue
        turnover = statistics.median(c * v for _, c, v in rows[-TURNOVER_WINDOW:])
        ranked.append({
            "insref":   insref,
            "ticker":   names.get(insref) or folder.split("_", 1)[1],
            "folder":   folder,
            "rows":     rows,
            "turnover": turnover,
        })
    ranked.sort(key=lambda u: -u["turnover"])
    universe = ranked[:UNIVERSE_SIZE]
    for i, u in enumerate(universe, 1):
        u["rank"] = i
    meta = {
        "eligible":   len(ranked),
        "size":       len(universe),
        "too_short":  short,
        "unreadable": unreadable,
        "stale":      stale,
        "history_through": newest.isoformat() if newest else None,
        "expected_through": want.isoformat(),
        "cutoff_msek": round(universe[-1]["turnover"] / 1e6, 1) if universe else None,
    }
    return universe, meta


# ── Signal ───────────────────────────────────────────────────────────────────

def _is_new_high(closes, i):
    """Did closes[i] beat the highest of the HIGH_LOOKBACK closes before it?
    None when fewer than HIGH_MIN_PRIOR prior closes exist — no verdict."""
    prior = closes[max(0, i - HIGH_LOOKBACK):i]
    if len(prior) < HIGH_MIN_PRIOR:
        return None
    return closes[i] > max(prior)


def breakout_check(closes):
    """(passes, info) for a close series ending with today's confirmed close.

    Passes when today is a new 250-session closing high AND none of the
    previous FRESH_GAP sessions was one. `info` carries the numbers the page
    shows: the prior high, how far through it today closed, how many sessions
    ago the last high was, and the 12-1 momentum used for priority."""
    n = len(closes)
    today_i = n - 1
    is_high = _is_new_high(closes, today_i)
    if is_high is None:
        return False, {"err": "insufficient history"}
    prior_high = max(closes[max(0, today_i - HIGH_LOOKBACK):today_i])
    last_high_ago = None
    for k in range(1, FRESH_GAP + 1):
        if _is_new_high(closes, today_i - k):
            last_high_ago = k
            break
    mom = (closes[-1 - MOM_SKIP] / closes[-1 - MOM_SPAN] - 1.0
           if n > MOM_SPAN else None)
    return bool(is_high and last_high_ago is None), {
        "close":         closes[-1],
        "prior_high":    prior_high,
        "breakout_pct":  closes[-1] / prior_high - 1.0,
        "new_high":      bool(is_high),
        "last_high_ago": last_high_ago,
        "mom_12_1":      mom,
    }


# ── Entry gate ───────────────────────────────────────────────────────────────

def close_confirmed_entry_ready(state, signals_state, today):
    """(ready, reason) for today's post-close entry pass.

    Same shape as live_cross's copy: our own `last_entry_scan_date` keeps the
    pass to once a day, and the feed-settled proof is borrowed from the Signals
    wallet, the only one that stamps it. No cutoff — see the module docstring.
    """
    iso = today.isoformat()
    if state.get("last_entry_scan_date") == iso:
        return False, "already scanned after today's close"
    now_local = datetime.now()
    if not L.is_trading_day(now_local):
        return False, ("weekend" if now_local.weekday() >= 5 else "market holiday")
    close_h, close_m = L.market_close_for(now_local)
    if now_local.hour * 60 + now_local.minute < close_h * 60 + close_m:
        half = " half-day" if L.is_early_close(now_local) else ""
        return False, f"before the {close_h:02d}:{close_m:02d}{half} close"
    if signals_state.get("last_ohlc_capture_date") != iso:
        return False, "feed has not settled past today's close yet"
    return True, ""


def exits_can_book(signals_state, today, now_local=None):
    """(ok, why) — may this tick book exits?

    Not until the feed is showing TODAY's session. live_cross stops at 09:00,
    but the feed runs ~15 minutes behind, so every quote between 09:00 and
    ~09:15 is still the previous close. For the 7-day engines that moves a
    TIMEOUT fill; here it would also arm the week-4 target on a stale price, or
    market-sell at a price that may never trade that day. Signals probes the
    lag on every market-hours tick and has saved it by the time this engine
    runs (data_lag.trade_dt, its last SEB A trade). Without a usable probe,
    wait until FEED_FALLBACK_OPEN_MIN. Waiting loses nothing: a stale quote is
    the price the previous evening's tick already judged.

    Off-hours on a trading day, and on non-trading days, the quote is the last
    close, which is exactly what an evening tick should book against."""
    if now_local is None:
        now_local = datetime.now()
    if not L.is_trading_day(now_local):
        return True, ""
    mins = now_local.hour * 60 + now_local.minute
    if mins < MARKET_OPEN_MIN:
        return False, (f"pre-open tick ({now_local.strftime('%H:%M')}) — the feed still "
                       f"carries the previous session's close")
    if not L.is_market_open(now_local):
        return True, ""
    trade_dt = None
    try:
        trade_dt = datetime.fromisoformat((signals_state.get("data_lag") or {}).get("trade_dt") or "")
    except (TypeError, ValueError):
        pass
    if trade_dt is not None:
        if trade_dt.date() >= today:
            return True, ""
        return False, (f"feed still on {trade_dt.date().isoformat()} — today's first prints "
                       f"arrive ~15 min after the open")
    if mins >= FEED_FALLBACK_OPEN_MIN:
        return True, ""
    return False, (f"no feed probe yet at {now_local.strftime('%H:%M')} — waiting until "
                   f"{FEED_FALLBACK_OPEN_MIN // 60:02d}:{FEED_FALLBACK_OPEN_MIN % 60:02d}, "
                   f"the feed runs ~15 min behind")


# ── Exits: the week-4 target ─────────────────────────────────────────────────

def _quote_is_live(lq):
    return (lq or {}).get("last") is not None and lq.get("source") not in ("csv-fallback", "stale")


def arm_targets(state, today, live):
    """Point each position's `tp` at what apply_exit_checks must act on this
    tick, and return {ticker: real tp} for restore_targets().

    apply_exit_checks has no notion of a target that switches on later, and it
    books every TP at exactly pos["tp"]. So, per position:
      · before `tp_live_from`: the target is parked out of reach;
      · on the first live look once it is on (`tp_armed` unset): a price
        already at or above it sells at MARKET — pos["tp"] is set to that
        price, so the limit cap books what the stock actually trades at;
      · from then on (`tp_armed`): the real target, limit-filled as usual.
    """
    saved = {}
    for tk, pos in state["positions"].items():
        saved[tk] = pos["tp"]
        if today < date.fromisoformat(pos["tp_live_from"]):
            pos["tp"] = TP_PARKED
        elif not pos.get("tp_armed"):
            lq = live.get(tk) or {}
            if _quote_is_live(lq) and lq["last"] >= pos["tp"]:
                pos["tp"] = lq["last"]
    return saved


def restore_targets(state, today, live, saved):
    """Undo arm_targets() on every position still open, and mark a target as
    armed once a live quote has seen it switched on — after that a crossing is
    an ordinary limit fill at +10%."""
    for tk, pos in state["positions"].items():
        if tk in saved:
            pos["tp"] = saved[tk]
        if (not pos.get("tp_armed")
                and today >= date.fromisoformat(pos["tp_live_from"])
                and _quote_is_live(live.get(tk))):
            pos["tp_armed"] = True


# ── Benchmark ────────────────────────────────────────────────────────────────

def update_benchmark(state, today, now_local):
    """OMX Stockholm PI since this wallet opened, so the page can show the line
    the strategy has to beat. Price index on purpose: the engine's closes and
    fills carry no dividends either.

    The base is an official CLOSE (`src: close`), taken on the first tick
    after a trading day's close and never moved after that. A base without
    that marker — brk-1's first one, read off the unrefreshed 11:00 History
    row — is replaced at the first close on or after its own date. Until a base
    exists the block says so instead of comparing against nothing. Fails soft:
    a benchmark must never stop a trade."""
    try:
        close_h, close_m = L.market_close_for(now_local)
        after_close = (L.is_trading_day(now_local)
                       and now_local.hour * 60 + now_local.minute >= close_h * 60 + close_m)
        # Outside the session the index cannot move, so one quote per completed
        # session is enough — not one every 3 minutes all night and weekend.
        session = (today if after_close else previous_session(today)).isoformat()
        prev = state.get("benchmark") or {}
        if not L.is_market_open(now_local) and prev.get("at_close_of") == session \
                and state.get("benchmark_base"):
            return
        q = L.get_live(INDEX_INSREF)
        if not q.get("ok") or not q.get("last") or q["last"] <= 0:
            prev["error"] = f"index quote unavailable ({q.get('err') or 'no price'}) — last good value kept"
            state["benchmark"] = prev
            return
        level = float(q["last"])
        base = state.get("benchmark_base") or {}
        if after_close and (not base or (base.get("src") != "close"
                                         and base.get("date", "") <= today.isoformat())):
            base = {"date": today.isoformat(), "level": level, "src": "close"}
            state["benchmark_base"] = base
        if not base:
            state["benchmark"] = {"name": INDEX_NAME, "date": today.isoformat(), "level": level,
                                  "error": "base is set at the first close — no comparison yet"}
            return
        state["benchmark"] = {
            "name":  INDEX_NAME,
            "from":  base["date"],
            "base":  base["level"],
            "date":  today.isoformat() if L.is_market_open(now_local) else session,
            "level": level,
            "ret":   level / base["level"] - 1.0,
        }
        if not L.is_market_open(now_local):
            state["benchmark"]["at_close_of"] = session
    except Exception as e:                      # noqa: BLE001
        state["benchmark"] = {"error": f"{type(e).__name__}: {e}"}


# ── Daily scan + entries ─────────────────────────────────────────────────────

def scan(today, out):
    """The once-a-day universe scan: rank, quote and test every name. Returns
    (candidates, live, meta), candidates sorted by priority. Gating happens in
    enter(), after the day's exits, so a slot or a name freed at today's close
    is available the same evening — as in the backtest."""
    universe, meta = build_universe(today)
    if history_behind(meta):
        # Checked before quoting: a held pass retries every tick until the
        # sweep catches up, and must not cost 150 quotes each time.
        meta.update({"quoted": 0, "no_quote": 0, "signals": 0})
        return [], {}, meta
    live = L.fetch_live_parallel([{"name": u["ticker"], "insref": u["insref"]}
                                  for u in universe]) if universe else {}
    candidates, no_quote = [], 0
    for u in universe:
        lq = live.get(u["ticker"]) or {}
        if not lq.get("ok") or not lq.get("last") or lq["last"] <= 0:
            no_quote += 1
            continue
        closes = [c for _, c, _ in u["rows"]] + [lq["last"]]
        ok, info = breakout_check(closes)
        if not ok:
            continue
        candidates.append({
            "ticker":        u["ticker"],
            "insref":        u["insref"],
            "ticker_path":   f"Equity/SEK/{u['folder']}",
            "rank":          u["rank"],
            "turnover_msek": round(u["turnover"] / 1e6, 1),
            **{k: info[k] for k in ("close", "prior_high", "breakout_pct", "mom_12_1")},
            "skip_reasons":  [],
            "blocked":       False,
        })
    # Strongest 12-1 momentum first — the order slots are filled in.
    candidates.sort(key=lambda c: -(c["mom_12_1"] if c["mom_12_1"] is not None else -9.0))
    meta.update({"quoted": len(universe) - no_quote, "no_quote": no_quote,
                 "signals": len(candidates)})
    out(f" Universe: {meta['size']} of {meta['eligible']} eligible names "
        f"(turnover cutoff {meta['cutoff_msek']} MSEK/day; {meta['too_short']} too short, "
        f"{meta['stale']} behind {meta['expected_through']}, {meta['unreadable']} unreadable) "
        f"— {meta['quoted']} quoted, {meta['signals']} fresh 52-week high(s)")
    return candidates, live, meta


def history_behind(meta):
    """True when more than MAX_STALE_SHARE of the rankable names stop before
    the previous session — the sign that app.py's sweep is not running."""
    ranked = meta["eligible"] + meta["stale"]
    return bool(ranked) and meta["stale"] / ranked > MAX_STALE_SHARE


def scan_problem(meta):
    """(reason, retry_capped) when the scan's inputs cannot be trusted, else
    (None, False). The pass is then held — nothing enters and the day is NOT
    stamped, so the next tick tries again.

      · History behind: more than MAX_STALE_SHARE of the names stop before the
        previous session — app.py's sweep is not running. Held for as long as
        that lasts: entering on a gapped window is worse than not entering.
      · Quotes missing: fewer than MIN_QUOTE_COVERAGE of the universe answered.
        Held for MAX_SCAN_ATTEMPTS tries, then the pass goes ahead with what
        answered, so a flaky API costs at most those names and never the day.
    """
    if history_behind(meta):
        return (f"price history behind — {meta['stale']} of {meta['eligible'] + meta['stale']} "
                f"names stop before {meta['expected_through']} (newest row "
                f"{meta['history_through']}); is app.py's sweep running?"), False
    if meta["size"] and meta["quoted"] / meta["size"] < MIN_QUOTE_COVERAGE:
        return f"only {meta['quoted']} of {meta['size']} names quoted", True
    return None, False


def enter(state, candidates, live, today, now_local_ts):
    """Fill candidates in priority order until slots or cash run out. Every
    candidate that does not fill gets its reason, so the page can say why."""
    entries = []
    open_val = sum(p["shares"] * ((live.get(tk) or {}).get("last") or p["entry_price"])
                   for tk, p in state["positions"].items())
    equity = state["cash"] + open_val
    target = equity / SLOTS
    floor  = target * POS_FLOOR_FRAC
    held_stems = {L.company_stem(tk) for tk in state["positions"]}
    for c in candidates:
        tk = c["ticker"]
        stem = L.company_stem(tk)
        why = None
        if ENTRIES_PAUSED:
            why = f"entries paused since {ENTRIES_PAUSED_SINCE or '?'}"
        elif tk in state["positions"]:
            why = "already held"
        elif stem in held_stems:
            why = f"duplicate stem '{stem}'"
        elif len(state["positions"]) >= SLOTS:
            why = f"all {SLOTS} slots in use"
        if why is None:
            lq = live.get(tk) or {}
            price = lq.get("last")
            liq = ec.liquidity_verdict(c["insref"], price, lq)
            cap, why = ec.apply_liquidity_cap(liq, target, floor)
        if why is None:
            shares, cost = ec.position_size(state["cash"], price, target, cap, floor)
            if shares <= 0:
                why = (f"cash {state['cash']:,.0f} below half a slot ({floor:,.0f})"
                       if state["cash"] < floor else
                       f"size below the {floor / 1000:.0f}k floor after rounding")
        if why:
            c["skip_reasons"].append(why)
            c["blocked"] = True
            continue
        tp_price = round(price * (1 + TP_PCT), 4)
        sl_price = round(price * (1 + SL_PCT), 4)
        tp_from  = L.add_trading_days(today, TP_FROM_SESSIONS)
        exit_by  = L.add_trading_days(today, MAX_HOLD_SESSIONS)
        state["cash"] -= cost
        state["positions"][tk] = {
            "insref":           c["insref"],
            "entry_price":      price,
            "shares":           shares,
            "entry_date":       today.isoformat(),
            "entry_ts":         now_local_ts,
            "tp":               tp_price,
            "tp_live_from":     tp_from.isoformat(),
            "tp_armed":         False,
            "sl":               sl_price,
            "target_exit_date": exit_by.isoformat(),
            "cost_sek":         cost,
            "signal_info": {
                "entry_tag":     "breakout",
                "conviction":    "normal",
                "pattern":       "52w high",
                "prior_high":    c["prior_high"],
                "breakout_pct":  c["breakout_pct"],
                "mom_12_1":      c["mom_12_1"],
                "universe_rank": c["rank"],
                "turnover_msek": c["turnover_msek"],
                "entered_at":    now_local_ts,
            },
            "fill_type":        "close",
            **V.stamp(ENGINE_KEY),
        }
        held_stems.add(stem)
        c["entered"] = True
        entries.append({"ticker": tk, "price": price, "shares": shares, "cost": cost,
                        "tp": tp_price, "tp_from": tp_from.isoformat(), "sl": sl_price,
                        "exit_by": exit_by.isoformat(), "c": c})
        notify.queue_buy(ENGINE_KEY, tk, price, shares, cost=cost, tp=tp_price, sl=sl_price,
                         exit_by=exit_by.isoformat(),
                         pattern=(f"New 52-week high (+{c['breakout_pct'] * 100:.1f}% through "
                                  f"{c['prior_high']:.2f}) · target live from {tp_from.isoformat()}"))
    return entries


# ── Main run ─────────────────────────────────────────────────────────────────

def main():
    rotate_log()
    log = open(LOG_PATH, "a", encoding="utf-8")

    def out(msg=""):
        log.write(msg + "\n")
        log.flush()

    state = load_state()
    now_local    = datetime.now()
    now_local_ts = now_local.replace(microsecond=0).isoformat()
    today        = ec.today_local()

    out("=" * 72)
    out(f" Live breakout run @ {now_local.strftime('%Y-%m-%d %H:%M:%S')} local")
    out("=" * 72)

    state["entries_paused"] = ENTRIES_PAUSED
    state["paused_since"]   = ENTRIES_PAUSED_SINCE  if ENTRIES_PAUSED else None
    state["paused_reason"]  = ENTRIES_PAUSED_REASON if ENTRIES_PAUSED else None
    update_benchmark(state, today, now_local)

    # ── 1. Is this the day's close-confirmed pass? ──────────────────────────
    signals_state = ec.read_json(SIGNALS_STATE_PATH, {})
    if L.is_market_open():
        entry_pass, gate_why = False, "intraday run — the scan and entries run after the close"
    else:
        entry_pass, gate_why = close_confirmed_entry_ready(state, signals_state, today)
    if entry_pass:
        out(" CLOSE-CONFIRMED ENTRY PASS — feed settled past today's close.")

    if not entry_pass and not state["positions"]:
        # Nothing to manage and nothing to buy. Both gate keys and exit_pass are
        # still written, so the page never shows an earlier tick's story under
        # this tick's timestamp.
        gate = {"open": False, "reason": gate_why, "date": today.isoformat(),
                "checked": now_local_ts, "entries": 0}
        state["entry_gate"] = gate
        state["last_entry_gate"] = gate
        state["exit_pass"] = {"ran": False, "reason": "no open positions",
                              "closed": 0, "checked": now_local_ts}
        state["force_sell_blocked"] = []
        save_state(state)
        out(f" No open positions, no entry pass: {gate_why}.")
        log.close()
        return

    # ── 2. Daily scan (entry pass only) ─────────────────────────────────────
    # A scan that raises or cannot trust its inputs HOLDS the pass: nothing
    # enters, the day is not stamped, the next tick retries — and the exits
    # below run regardless, so a bad file can never leave the book unmanaged.
    candidates, live, meta = [], {}, None
    if entry_pass:
        attempt = state.get("scan_attempts") or {}
        attempt = attempt if attempt.get("date") == today.isoformat() else {"date": today.isoformat(), "n": 0}
        attempt["n"] += 1
        state["scan_attempts"] = attempt
        try:
            candidates, live, meta = scan(today, out)
            problem, capped = scan_problem(meta)
        except Exception as e:                  # noqa: BLE001
            candidates, live, meta = [], {}, None
            problem, capped = f"scan failed — {type(e).__name__}: {e}", False
        if problem and not (capped and attempt["n"] >= MAX_SCAN_ATTEMPTS):
            entry_pass, candidates = False, []
            gate_why = f"{problem} — pass held, retrying next tick (attempt {attempt['n']})"
            out(f" ENTRY PASS HELD: {gate_why}.")
        elif problem:
            meta["warning"] = f"{problem}; scanned anyway after {attempt['n']} attempts"
            out(f" {meta['warning']}.")

    # ── 3. Quotes for the open book ─────────────────────────────────────────
    missing = [{"name": tk, "insref": p.get("insref")} for tk, p in state["positions"].items()
               if p.get("insref") and not (live.get(tk) or {}).get("ok")]
    if missing:
        live.update(L.fetch_live_parallel(missing))
    for tk, pos in state["positions"].items():
        lq = live.get(tk)
        if not lq or not lq.get("ok") or not lq.get("last") or lq["last"] <= 0:
            # A missing or non-positive quote must never book a price exit.
            live[tk] = {"last": pos["entry_price"], "ok": True, "source": "stale"}

    # ── 4. Exits — SL from day one, TP from week 4, TIMEOUT at 12 weeks ─────
    force_sells = L.load_force_sells(FORCE_SELL_PATH)
    exits_ok, exits_why = exits_can_book(signals_state, today, now_local)
    closed_now = []
    if not exits_ok:
        queued = sorted(tk for tk in force_sells if tk in state["positions"])
        for tk in queued:
            out(f"   MANUAL sell for {tk} DEFERRED — {exits_why}; stays queued "
                f"until the feed shows today's session")
        state["force_sell_blocked"] = [{"ticker": tk, "reason": exits_why,
                                        "source": "pre-open", "ts": now_local_ts}
                                       for tk in queued]
        out(f" Exits deferred — {exits_why}.")
    else:
        saved = arm_targets(state, today, live)
        try:
            closed_now = L.apply_exit_checks(state, today, now_local_ts, live, force_sells,
                                             engine=ENGINE_KEY)
        finally:
            restore_targets(state, today, live, saved)
        # reconcile, not consume: a "Sell now" queued against a position this
        # very tick closed at TP/SL/TIMEOUT is pruned (and named), instead of
        # staying armed to market-sell the next position opened in that name.
        L.reconcile_force_sells(state, closed_now, force_sells,
                                path=FORCE_SELL_PATH, engine=ENGINE_KEY)
    if closed_now:
        out(f"\n--- CLOSED THIS RUN ({len(closed_now)}) ---")
        for t in closed_now:
            out(f"  [BRK] {t['ticker']:<22} {t['reason']:<7} @ {t['exit_price']:.2f}   "
                f"P&L {t['pnl_sek']:+,.0f} SEK ({t['ret_pct'] * 100:+.2f}%)   "
                f"opened {t['entry_date']}")

    # ── 5. Entries ──────────────────────────────────────────────────────────
    entries = []
    if entry_pass:
        if ENTRIES_PAUSED:
            out(" ENTRIES PAUSED (ENTRIES_PAUSED=True) — exits, scan and reporting still run.")
        entries = enter(state, candidates, live, today, now_local_ts)
        state["last_scan"] = {"date": today.isoformat(), "checked": now_local_ts,
                              "universe": meta, "candidates": candidates}
        # Every position starts life as a candidate, so this also covers the
        # click-through for the open book.
        paths = state.setdefault("ticker_paths", {})
        for c in candidates:
            paths.setdefault(c["ticker"], c["ticker_path"])

    # ── 6. Reporting ────────────────────────────────────────────────────────
    open_value = sum(p["shares"] * ((live.get(tk) or {}).get("last") or p["entry_price"])
                     for tk, p in state["positions"].items())
    equity   = state["cash"] + open_value
    realized = sum(t["pnl_sek"] for t in state["closed_trades"])
    out(f"\nWallet:    cash {state['cash']:>12,.0f}   open {open_value:>12,.0f}   "
        f"equity {equity:>12,.0f}  SEK")
    out(f"P&L:       realized {realized:+,.0f} SEK   open positions "
        f"{len(state['positions'])}/{SLOTS}   closed trades {len(state['closed_trades'])}")
    bm = state.get("benchmark") or {}
    if bm.get("ret") is not None:
        ws = ec.wallet_start(state, WALLET_START)
        out(f"Benchmark: wallet {equity / ws - 1:+.2%} vs {bm['name']} {bm['ret']:+.2%} "
            f"since {bm['from']}")
    if entries:
        out(f"\n--- NEW BREAKOUT ENTRIES ({len(entries)}) ---")
        for e in entries:
            c = e["c"]
            mom = f"{c['mom_12_1'] * 100:+.0f}%" if c["mom_12_1"] is not None else "?"
            out(f"  BUY [BRK] {e['ticker']:<22} {e['shares']:>6} sh @ {e['price']:.2f}  "
                f"= {e['cost']:>9,.0f} SEK   +{c['breakout_pct'] * 100:.1f}% through "
                f"{c['prior_high']:.2f}   12-1 {mom}   rank {c['rank']}")
            out(f"      target {e['tp']:.2f} (+{TP_PCT * 100:.0f}%, live from {e['tp_from']})  "
                f"stop {e['sl']:.2f} ({SL_PCT * 100:+.0f}%)  exit by {e['exit_by']}")
    blocked = [c for c in candidates if c["blocked"]]
    if blocked:
        out(f"\n--- NOT BOUGHT ({len(blocked)}) ---")
        for c in blocked[:15]:
            out(f"  skipped {c['ticker']:<22} {' · '.join(c['skip_reasons'])}")
        if len(blocked) > 15:
            out(f"  …+{len(blocked) - 15} more")
    ec.report_open(state, live, out, "  [BRK] ")

    # ── 7. Persist what the page reads ──────────────────────────────────────
    quote_tickers = set(state["positions"]) | {c["ticker"] for c in candidates}
    state["last_quotes"] = ec.build_quote_snapshot(quote_tickers, live,
                                                   state.get("last_quotes") or {}, "live-breakout")
    state["last_entry_gate"] = {
        "open":    bool(entry_pass and not ENTRIES_PAUSED),
        "reason":  (f"entries paused since {ENTRIES_PAUSED_SINCE or '?'}" if ENTRIES_PAUSED
                    else gate_why),
        "date":    today.isoformat(),
        "checked": now_local_ts,
        "entries": len(entries),
    }
    state["entry_gate"] = state["last_entry_gate"]
    state["exit_pass"] = {"ran": exits_ok, "reason": exits_why or "ok",
                          "closed": len(closed_now), "checked": now_local_ts}
    # Stamped last, so a crash mid-pass leaves the day unscanned and the next
    # post-close tick retries it rather than silently losing the day.
    if entry_pass:
        state["last_entry_scan_date"] = today.isoformat()

    save_state(state)
    notify.flush(out=out)     # only after the wallet is written — see lib/notify.py
    log.close()


if __name__ == "__main__":
    main()
