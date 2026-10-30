"""Shared helpers for the three paper-trade engines (live_signals,
live_patterns, live_cross).

Previously each engine carried its own byte-for-byte copies of state I/O, log
rotation and the Friday / late-entry gates. Consolidating them here removes the
drift risk between engines that are supposed to share identical semantics, and
lets two latent bugs be fixed once:

  * write_json_atomic() — the engines used to ``json.dump`` straight onto the
    live wallet file. A crash mid-write (launchd timeout, laptop sleep) left a
    truncated file that ``load_state`` then refused to parse, bricking the
    wallet of record. Writing to a temp file and ``os.replace``-ing it is
    atomic, so a partial write can never clobber the good file.

  * today_local() — entry/exit dates and the cooldown/timeout math used the UTC
    date while the Friday / market-hours gates used local time. The Millistream
    feed reports Stockholm-local time, so every engine derives "today" from one
    local clock here.
"""

import csv
import io
import json
import os
import statistics
import tempfile
from datetime import datetime, timezone, timedelta


# ── JSON I/O ─────────────────────────────────────────────────────────────────

def read_json(path, default):
    """Guarded read — returns ``default`` if the file is missing or unreadable."""
    if not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def write_json_atomic(path, obj, **dump_kwargs):
    """Dump ``obj`` to a temp file in the same directory, then atomically
    ``os.replace`` it onto ``path``. A crash partway through can no longer
    truncate the destination."""
    dump_kwargs.setdefault("indent", 2)
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, **dump_kwargs)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_text_atomic(path, text, encoding="utf-8", newline=""):
    """Write ``text`` to a temp file in the same directory, then atomically
    ``os.replace`` it onto ``path``.

    The CSV counterpart of write_json_atomic(). Several writers used to open the
    real file in "w" mode — which truncates it *before* the new content is
    known to be good — so a mid-write crash, an empty upstream fetch or a raised
    parser left the destination short or empty. For History.csv and the
    per-stock OHLC bars that file is the only copy of data going back years.
    Building the full replacement first and swapping it in means the destination
    is only ever the old file or the complete new one."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    # suffix is deliberately NOT ".csv": these land in the per-stock
    # Instrumenttype/**/<stock>/ folders, which app.py enumerates generically by
    # extension (a folder counts as a stock if it holds any *.csv, and the /files
    # listing shows every *.csv). os.listdir doesn't hide dotfiles, so a ".csv"
    # temp name would show up mid-write — and stay forever if the process is
    # killed between mkstemp() and os.replace(), the exact case this helper is
    # here for. Nothing reads the temp name; only the final os.replace matters.
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline=newline) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_rows_atomic(path, rows, encoding="utf-8"):
    """Atomically write an iterable of row-sequences as CSV. Thin wrapper over
    write_text_atomic() so callers don't hand-join fields and lose quoting."""
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    write_text_atomic(path, buf.getvalue(), encoding=encoding)



# ── Wallet state ─────────────────────────────────────────────────────────────

def load_state(path, default_skeleton):
    """Load an engine wallet, or return a fresh copy of ``default_skeleton`` if
    the file does not exist yet.

    A file that *exists but is corrupt* is deliberately allowed to raise rather
    than be swallowed: silently resetting the wallet of record would hide real
    data loss. write_json_atomic() prevents the truncation that used to cause
    this in the first place."""
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return dict(default_skeleton)


SNAPSHOT_RETAIN_DAILIES = 30


def _prune_snapshots(backups_dir, prefix, retain):
    """Keep the newest ``retain`` dailies plus the first snapshot of each month,
    delete the rest — the same retention shape as rotate_log(). Enough recent
    detail to undo a bad script, plus a thin monthly spine so a mistake noticed
    long after the fact still has something older than the window to diff
    against."""
    days = []
    for fname in os.listdir(backups_dir):
        if not (fname.startswith(prefix) and fname.endswith(".json")):
            continue
        day = fname[len(prefix):-len(".json")]
        try:
            datetime.strptime(day, "%Y-%m-%d")
        except ValueError:
            continue   # hand-made backups (.pre_deposit, .bak.*) are not ours to prune
        days.append(day)
    days.sort()
    keep = set(days[-retain:])
    monthly = {}
    for day in days:
        monthly.setdefault(day[:7], day)
    keep.update(monthly.values())
    for day in days:
        if day in keep:
            continue
        try:
            os.unlink(os.path.join(backups_dir, prefix + day + ".json"))
        except OSError:
            pass


def snapshot_state(path, backups_dir=None, retain=SNAPSHOT_RETAIN_DAILIES):
    """Copy the wallet at ``path`` to
    ``state/backups/daily/<name>.<YYYY-MM-DD>.json`` once per local day, taken
    *before* the day's first overwrite.

    The wallet files are the only copy of every trade the engines have ever
    booked: ``state/`` is gitignored so no version history exists, and
    save_state() rewrites each file in full every three minutes.
    write_json_atomic() protects against a *torn* write, which is a different
    failure from a *wrong* one — nothing stopped a maintenance script or a bug
    in a mutation path from committing structurally-valid but wrong state, and
    this repo has already rewritten the historical record in place three times
    (retighten_stops, backfill_versions, the pre_stale_reclass relabel), each
    protected only by a human remembering to copy the file first. A daily
    pre-write snapshot turns "noticed a week later" from unrecoverable into at
    most one day of lost book.

    Returns the path of today's snapshot — whether taken now or earlier in the
    day — or None if there is no wallet on disk yet to copy. Cost on the
    3-minute hot path is one os.path.exists(): the copy happens on the first
    save of each day only."""
    if not os.path.exists(path):
        return None                     # brand-new wallet — nothing to preserve yet
    name   = os.path.basename(path)
    stem   = name[:-len(".json")] if name.endswith(".json") else name
    prefix = stem + "."
    if backups_dir is None:
        backups_dir = os.path.join(os.path.dirname(os.path.abspath(path)),
                                   "backups", "daily")
    dest = os.path.join(backups_dir, prefix + today_local().isoformat() + ".json")
    if os.path.exists(dest):
        return dest
    os.makedirs(backups_dir, exist_ok=True)
    with open(path, "rb") as f:
        payload = f.read()
    # Parse before enshrining: snapshotting an already-broken file would burn
    # the day's only recovery point, so an unparseable source raises instead.
    json.loads(payload.decode("utf-8"))
    fd, tmp = tempfile.mkstemp(dir=backups_dir, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _prune_snapshots(backups_dir, prefix, retain)
    return dest


def save_state(path, state, in_session=False):
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    # last_session_run moves only on a tick made while the exchange is open, and
    # is what /live-health judges a past session on. last_run moves on every
    # tick around the clock, so one post-close tick from a Mac waking briefly
    # made an engine that slept through the whole session read as current.
    if in_session:
        state["last_session_run"] = state["last_run"]
    # Snapshot before the overwrite, so the copy is the last known-good book
    # rather than whatever this run is about to write. A backup that quietly
    # stopped running is the exact failure it exists to guard against, so the
    # reason is persisted into the wallet (and cleared on the next success)
    # instead of being swallowed — a save must still go through, the wallet of
    # record cannot be held hostage to a full disk.
    try:
        dest = snapshot_state(path)
        if dest:
            state["last_snapshot"] = os.path.basename(dest)
        state.pop("snapshot_error", None)
    except Exception as exc:
        state["snapshot_error"] = "%s: %s" % (type(exc).__name__, exc)
    write_json_atomic(path, state, default=str)


def wallet_start(state, default):
    """Capital contributed to this wallet — the opening balance plus any later
    top-ups, NOT the current cash.

    Return% is measured against this rather than a hardcoded constant, so a
    deposit doesn't read as profit: paying 500k into a wallet would otherwise
    show up as +100% on the equity tile. `deposits` in the same state file is
    the audit trail of where the number came from. Absent (every wallet opened
    before 2026-08-24) means it was never topped up, so the engine's own
    WALLET_START is still correct."""
    v = state.get("wallet_start")
    return float(v) if isinstance(v, (int, float)) and v > 0 else float(default)


# ── Sector tags ──────────────────────────────────────────────────────────────

_ROOT_DIR       = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_NAMES_CSV_PATH = os.path.join(_ROOT_DIR, "stock_names.csv")
_sector_cache   = {"mtime": None, "map": {}}


def sector_map(path=None):
    """{ticker_name: sector_name} from stock_names.csv column 5, mtime-cached.

    Populated by scripts/tag_sectors.py from the feed's numeric `sector` /
    `sectorname` pair. Keyed by display name because that is what the engines
    write into their state and what the live pages render — same convention as
    app._live_tier_map.

    Empty dict when the file is missing or has never been tagged; every caller
    must treat a missing sector as "unknown" and carry on. The tag is
    informational only — no entry or exit decision depends on it.
    """
    path = path or _NAMES_CSV_PATH
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    if _sector_cache["mtime"] == mtime:
        return _sector_cache["map"]
    out = {}
    try:
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.reader(f):
                if len(row) >= 6:
                    nm, sec = row[1].strip(), row[5].strip()
                    if nm and sec:
                        out[nm] = sec
    except OSError:
        return _sector_cache["map"]
    _sector_cache["mtime"] = mtime
    _sector_cache["map"]   = out
    return out


def sector_concentration(tickers, costs=None, path=None):
    """Group `tickers` by sector for logging and the UI.

    Returns [{sector, tickers, n, cost}] sorted by exposure, heaviest first.
    `costs` maps ticker -> SEK at risk; when omitted the rows carry cost 0.0 and
    only the counts are meaningful. Untagged names collapse into "(unknown)"
    rather than being dropped, so a gap in the tags can never make a
    concentration look smaller than it is.
    """
    smap  = sector_map(path)
    costs = costs or {}
    groups = {}
    for tk in tickers:
        sec = smap.get(tk) or "(unknown)"
        g = groups.setdefault(sec, {"sector": sec, "tickers": [], "n": 0, "cost": 0.0})
        g["tickers"].append(tk)
        g["n"]    += 1
        g["cost"] += float(costs.get(tk) or 0.0)
    return sorted(groups.values(), key=lambda g: (-g["cost"], -g["n"], g["sector"]))


# ── Date / entry gates ───────────────────────────────────────────────────────

def today_local():
    """Stockholm-local trade date — the single clock all engines stamp against."""
    return datetime.now().date()


def is_friday(today=None):
    return (today or today_local()).weekday() == 4


def past_cutoff(cutoff_minutes):
    """True once the local wall-clock is at/after ``cutoff_minutes`` past
    midnight (e.g. 16*60+30 for a 16:30 late-entry cutoff)."""
    now = datetime.now()
    return (now.hour * 60 + now.minute) >= cutoff_minutes


# ── Log rotation ─────────────────────────────────────────────────────────────

def rotate_log(log_path, marker, retain_hours=24, min_bytes=200_000):
    """Trim ``log_path`` to roughly the last ``retain_hours`` of output, keeping
    whole runs delimited by header lines containing ``marker`` (e.g.
    ``" Live signal run @ "``). No-op below ``min_bytes`` so a frequent cron
    isn't doing pointless I/O. Truncates the same inode in place so launchd's
    open file descriptor keeps appending at the new end."""
    try:
        size = os.path.getsize(log_path)
    except OSError:
        return
    if size < min_bytes:
        return
    cutoff = (datetime.now() - timedelta(hours=retain_hours)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return
    keep_from = None
    for i, ln in enumerate(lines):
        if marker in ln:
            ts_str = ln.split("@", 1)[1].strip().replace(" local", "").strip()
            if ts_str >= cutoff:
                keep_from = i
                break
    if keep_from is None:
        # No run within the retention window — file is pure history, drop it.
        try:
            with open(log_path, "wb"):
                pass
        except OSError:
            pass
        return
    if keep_from == 0:
        return  # already starts within the window
    # Keep the `===` banner immediately before the first surviving run header.
    if lines[keep_from - 1].startswith("==="):
        keep_from -= 1
    try:
        with open(log_path, "wb") as f:
            f.write("".join(lines[keep_from:]).encode("utf-8"))
    except OSError:
        pass


# ── Entry gating / sizing (shared by all three engines) ──────────────────────

def build_recent_exits(state):
    """Map ticker -> (most_recent_exit_date_str, reason) from closed_trades, for
    the post-exit re-entry cooldown. Includes exits closed earlier in the same
    run (already appended to closed_trades by apply_exit_checks), so an SL/TP at
    15:19 can't be immediately re-bought at 15:19."""
    recent = {}
    for tr in state.get("closed_trades", []):
        ex = tr.get("exit_date")
        if not ex:
            continue
        cur = recent.get(tr["ticker"])
        if cur is None or ex > cur[0]:
            recent[tr["ticker"]] = (ex, tr.get("reason"))
    return recent


def cooldown_status(recent_exits, ticker, today, cooldown_days, trading_days_fn):
    """Decide whether `ticker` may re-enter today. Returns None when clear, else
    a verdict dict {kind, tds, last_date, last_reason}:
      * kind='SL'       — stop-loss exit < cooldown_days trading days ago
      * kind='same-day' — TP/TIMEOUT exit earlier today (next trading day is OK)
    The caller formats its own skip message from the verdict. `trading_days_fn`
    is live_signals.trading_days_between, passed in to avoid a circular import."""
    last = recent_exits.get(ticker)
    if not last:
        return None
    last_date, last_reason = last
    last_d = (datetime.fromisoformat(last_date).date()
              if isinstance(last_date, str) else last_date)
    if last_reason == "SL":
        tds = trading_days_fn(last_d, today)
        if tds < cooldown_days:
            return {"kind": "SL", "tds": tds, "last_date": last_date, "last_reason": last_reason}
        return None
    if last_d >= today:
        return {"kind": "same-day", "tds": None, "last_date": last_date, "last_reason": last_reason}
    return None


def position_size(cash, price, target, cap, floor):
    """Whole-share sizing: ``min(target, cash, cap) // price``. Returns
    (shares, cost), or (0, 0.0) when the rounded position is unaffordable or
    below ``floor`` SEK — the caller decides what to do with the (0, 0.0) case
    (its skip message / order-drop differs per engine)."""
    size = min(target, cash, cap)
    shares = int(size // price)
    cost = shares * price
    if shares <= 0 or cost < floor:
        return 0, 0.0
    return shares, cost


# ── Liquidity gate (added 2026-08-24) ────────────────────────────────────────
#
# None of the engines used to ask whether a fill was physically possible. On
# 2026-08-24 live_patterns bought 2,380 shares of Electrolux A — 53% of that
# name's entire day volume (4,460 shares; the four prior sessions printed 332,
# 3,035, 3,949 and one 463,867-share block). The quote sat at bid=ask=last for
# the rest of the session, so the position could be neither marked nor exited,
# and would have booked a fictional P&L. A-class Nordic listings do this
# routinely: Electrolux A, Bilia A, Husqvarna A, SEB A/C all trade a few
# hundred shares on a normal day while their B-class twins trade millions.

OHLC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ohlc")

# 5% of median daily volume clears every real entry the three engines have ever
# taken by a wide margin — the worst legitimate one is 1.34% (Atrium Ljungberg B
# on 2026-08-24) — while rejecting the Electrolux A fill at 60.3% of its median
# day. That is a 3.7x headroom over the worst genuine trade.
MAX_PARTICIPATION = 0.05
LIQ_LOOKBACK_DAYS = 20
LIQ_MIN_BARS      = 3


def median_daily_volume(insref, lookback_days=LIQ_LOOKBACK_DAYS,
                        min_bars=LIQ_MIN_BARS, ohlc_dir=None):
    """Median full-day share volume for `insref` from ``ohlc/{insref}_OHLC.csv``.

    Only bars with volume > 0 count. The 2026-06-13 backfill derived its ~4,700
    bars from minute closes and wrote volume=0 for every one of them, so an
    unfiltered average would read as "nearly untraded" for all 980 stocks.

    Median, not mean: one block trade (Electrolux A printed 463,867 shares on
    2026-08-19 against a ~4,000-share norm) would otherwise certify a dead
    ticker as liquid for the rest of the lookback window.

    Returns None when fewer than `min_bars` real bars exist. The caller must
    read that as "no opinion", never as "illiquid" — real volume capture only
    began 2026-08-18 and still covers only the large-cap universe.
    """
    if not insref:
        return None
    try:
        path = os.path.join(ohlc_dir or OHLC_DIR, f"{int(insref)}_OHLC.csv")
    except (TypeError, ValueError):
        return None
    vols = []
    try:
        with open(path, encoding="utf-8") as f:
            for row in csv.reader(f):
                if len(row) >= 6 and row[0]:
                    try:
                        v = float(row[5])
                    except ValueError:
                        continue
                    if v > 0:
                        vols.append(v)
    except OSError:
        return None
    vols = vols[-lookback_days:]
    if len(vols) < min_bars:
        return None
    return statistics.median(vols)


def liquidity_verdict(insref, price, live_quote=None,
                      max_participation=MAX_PARTICIPATION,
                      lookback_days=LIQ_LOOKBACK_DAYS,
                      min_bars=LIQ_MIN_BARS, ohlc_dir=None):
    """How large a position this name can absorb, in SEK.

    Returns ``{"cap_sek", "basis", "volume", "note"}``:

      * ``basis='history'``  — median daily volume from ``ohlc/``. The real gate.
      * ``basis='intraday'`` — no usable history, so fall back to what has
        actually printed today. The quote's ``quantity`` field is the day's
        cumulative share count (verified against the EOD capture: Electrolux B
        quantity=2,738,744 == its 2026-08-24 OHLC volume). Capped at 100% of
        that rather than `max_participation`, because intraday volume is a
        partial day — at 09:30 five percent of it would reject the whole
        universe. "You may not buy more shares than have traded all session" is
        a weaker claim but a true one at any hour, and it still stops the
        Electrolux A case (2,380 shares against 4,460 for the full day).
      * ``basis='none'``     — no volume data at all. ``cap_sek`` is None and the
        caller must **not** block: the 240-name watchlist has no captured
        history yet, so treating absence as illiquidity would silently halt half
        the engine — exactly the silent-failure shape this project keeps hitting.
    """
    if not price or price <= 0:
        return {"cap_sek": None, "basis": "none", "volume": None, "note": "no price"}

    med = median_daily_volume(insref, lookback_days, min_bars, ohlc_dir)
    if med is not None:
        cap = med * max_participation * price
        return {"cap_sek": cap, "basis": "history", "volume": med,
                "note": (f"{med:,.0f} sh/day median, "
                         f"{max_participation * 100:.0f}% cap = {cap / 1000:,.0f}k")}

    today_vol = None
    if isinstance(live_quote, dict):
        try:
            today_vol = float(live_quote.get("quantity") or 0) or None
        except (TypeError, ValueError):
            today_vol = None
    if today_vol:
        cap = today_vol * price
        return {"cap_sek": cap, "basis": "intraday", "volume": today_vol,
                "note": f"{today_vol:,.0f} sh traded today = {cap / 1000:,.0f}k"}

    return {"cap_sek": None, "basis": "none", "volume": None,
            "note": "no volume history"}


def apply_liquidity_cap(verdict, cap_sek, floor_sek):
    """Fold a liquidity verdict into the engine's own size cap.

    Returns ``(effective_cap, skip_reason)``. `skip_reason` is None when the
    name can carry at least `floor_sek`; otherwise the position is not worth
    taking at any size the engine is willing to hold, and the caller skips.
    A verdict with no data leaves the engine's own cap untouched.
    """
    liq = verdict.get("cap_sek")
    if liq is None:
        return cap_sek, None
    if liq < floor_sek:
        return cap_sek, f"illiquid: {verdict['note']} < {floor_sek / 1000:.0f}k floor"
    return min(cap_sek, liq), None


# ── Cross-wallet dedupe (added 2026-08-24) ───────────────────────────────────
#
# Each engine deduped share classes only against its *own* book, so 2026-08-24
# closed with Electrolux B in Signals and Electrolux A in Patterns (140k of one
# company), and Securitas B held in both Patterns and Cross. Signals had already
# rejected Electrolux A itself as a "duplicate of held 'Electrolux'" — it simply
# had no way to see what the other two wallets were holding.
#
# NOTE this deliberately couples the three wallets: a Patterns entry now depends
# on what Signals happens to hold, so the wallets are no longer independent
# strategy samples. That is a real cost to the A/B comparison they were built
# for, accepted because the aggregate book is one pot of money. Set
# CROSS_WALLET_DEDUPE=False in any engine to restore independence.
#
# Live Breakout (live_breakout.py, 2026-10-26) is deliberately NOT in this map,
# which is what keeps it out of the dedupe in BOTH directions: Signals and
# Patterns build their sibling sets from here, so they never see its book, and
# it never reads theirs (its own CROSS_WALLET_DEDUPE is False). Owner decision:
# a 12-week position must not block a 7-day engine from a company for 12 weeks,
# or the reverse, or no wallet trades the rules it was tested on. Adding it here
# would silently re-couple it — list it in app.py / lib/notify.py instead.

WALLET_FILES = {
    "signals":  ("Signals",  "live_portfolio.json"),
    "patterns": ("Patterns", "live_patterns_portfolio.json"),
    "cross":    ("Cross",    "live_cross_portfolio.json"),
}


def sibling_wallets(engine_key, state_dir=None):
    """[(label, path), ...] for the *other* two wallets.

    Hardcoded rather than read from CHANGELOG.json's `engines` map: the ledger
    is documentation and must never be able to gate a trade (see lib/version.py).
    """
    if state_dir is None:
        state_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "state")
    return [(label, os.path.join(state_dir, fname))
            for key, (label, fname) in WALLET_FILES.items() if key != engine_key]


def foreign_held_stems(wallet_paths, stem_fn, states=None):
    """``stem -> "Label: ticker"`` for every position held in another wallet.

    `states` optionally supplies an already-loaded state per label, so an engine
    that has read a sibling wallet for other reasons (live_cross reads both
    upstream wallets) does not read it twice.

    Fails soft: an unreadable or half-written sibling contributes nothing rather
    than raising. A dedupe gate must never be able to stop the engine that owns
    the trade.
    """
    out = {}
    for label, path in wallet_paths:
        st = (states or {}).get(label)
        if st is None:
            st = read_json(path, None)
        if not isinstance(st, dict):
            continue
        for tk in (st.get("positions") or {}):
            out.setdefault(stem_fn(tk), f"{label}: {tk}")
    return out


def build_quote_snapshot(tickers, live, prior_quotes, source_label=None):
    """Build the UI `last_quotes` map for `tickers`. A good live quote (last
    present, source not csv-fallback/stale) is emitted compactly; otherwise the
    prior run's quote is kept and re-tagged 'stale', so the UI never shows a
    synthetic/placeholder price as 'Current'. `source_label` overrides the
    emitted source tag (engines pass 'live-patterns'/'live-cross'); None keeps
    the quote's own source (live_signals)."""
    snap = {}
    for name in tickers:
        lq = live.get(name) or {}
        if lq.get("last") is not None and lq.get("source") not in ("csv-fallback", "stale"):
            snap[name] = {
                "last":     lq.get("last"),
                "bid":      lq.get("bid"),
                "ask":      lq.get("ask"),
                "diff_pct": lq.get("diffprc"),
                "source":   source_label if source_label is not None else lq.get("source"),
            }
        elif name in prior_quotes:
            kept = dict(prior_quotes[name])
            kept["source"] = "stale"
            snap[name] = kept
    return snap


def report_open(state, live, out, tag):
    """Print the open-positions block to the run log. `tag` is the per-engine
    row prefix including its padding (e.g. '  [PAT]     ' / '  [X]  ')."""
    if not state["positions"]:
        return
    out(f"\n--- OPEN POSITIONS ({len(state['positions'])}) ---")
    for tk, pos in state["positions"].items():
        cur = live.get(tk, {}).get("last") or pos["entry_price"]
        pnl = pos["shares"] * (cur - pos["entry_price"])
        ret = cur / pos["entry_price"] - 1
        pat = (pos.get("signal_info") or {}).get("pattern", "?")
        out(f"{tag}{tk:<22} {pos['shares']:>5} sh   "
            f"entered {pos['entry_date']} @ {pos['entry_price']:.2f}   "
            f"now {cur:.2f}   P&L {pnl:+,.0f} SEK ({ret*100:+.2f}%)   {pat}")
        out(f"                          TP {pos['tp']:.2f}   SL {pos['sl']:.2f}   "
            f"exit by {pos['target_exit_date']}")
