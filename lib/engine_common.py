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

import json
import os
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


def save_state(path, state):
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    write_json_atomic(path, state, default=str)


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
