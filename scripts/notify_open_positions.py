#!/usr/bin/env python3
"""Push a snapshot of every open position across the three wallets.

Written as the end-to-end test of the notification path added in 0.21.0: the
Settings page's Test button sends invented data through the sinks, which proves
the plumbing but tells you nothing about how a real position reads on a phone.
This sends the actual book.

Deliberately NOT phrased as a fill. queue_buy/queue_sell put "bought"/"sold" in
the title, which on the phone means money moved; a snapshot borrowing that shape
would be indistinguishable from a real trade. These go out through
queue_message() with a 🧪 marker and the verb "holds".

Read-only: it opens the three wallet files and never writes them, so it is safe
to run while the engines are mid-tick.

Usage:
    python3 scripts/notify_open_positions.py            # send
    python3 scripts/notify_open_positions.py --dry-run  # print, send nothing
"""

import os
import sys
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "lib"))

import env_loader                                        # noqa: E402
env_loader.load_env()

import engine_common as ec                               # noqa: E402
import notify                                            # noqa: E402

WALLETS = (
    ("signals",  "live_portfolio.json"),
    ("patterns", "live_patterns_portfolio.json"),
    ("cross",    "live_cross_portfolio.json"),
)


def _held_for(entry_ts):
    """Same shape as the notification's 'held' line — see hold_duration()."""
    try:
        delta = datetime.now() - datetime.fromisoformat(str(entry_ts))
    except (TypeError, ValueError):
        return "—"
    sec = max(0, int(delta.total_seconds()))
    if sec >= 86400:
        return f"{round(sec / 86400)}d"
    if sec >= 3600:
        return f"{sec // 3600}h {(sec % 3600) // 60}m"
    return f"{sec // 60}m"


# The LaunchAgent ticks every 3 minutes and stamps last_run on every path,
# including the market-closed early return — so a stamp older than this many
# ticks means the *process* stopped, not that the market is shut. Ten ticks is
# slack for a slow sweep; the failures this exists to catch (the 6-second
# close-gate bug, launchd EX_CONFIG exit 78) leave it hours or days behind.
STALE_RUN_SECONDS = 10 * 180


def _ago(sec):
    """Coarse '3d 2h' / '2h 5m' / '7m' — same shape as the held line."""
    sec = int(sec)
    if sec >= 86400:
        return f"{sec // 86400}d {(sec % 86400) // 3600}h"
    if sec >= 3600:
        return f"{sec // 3600}h {(sec % 3600) // 60}m"
    return f"{sec // 60}m"


def _run_age(ts):
    """Seconds since an ISO stamp, or None if it cannot be read. last_run is
    written timezone-aware (engine_common.stamp) while entry_ts is naive local,
    so match the stamp's own awareness before subtracting."""
    try:
        dt = datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return None
    now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
    return max(0.0, (now - dt).total_seconds())


def collect():
    """([(engine, ticker, position, last_quote, run_age)], [alert, ...]).

    Rows are newest entry first. ``run_age`` is the age in seconds of that
    wallet's last_run (None if unknown): a quote carries source == "live"
    forever, so it proves the last fetch succeeded, not that it was recent.
    ``alerts`` are wallet-level problems that must be said out loud — a dead
    engine or an unreadable wallet, either of which would otherwise render as a
    confident, wholly stale book."""
    rows, alerts = [], []
    for key, fname in WALLETS:
        label = notify.ENGINE_LABELS.get(key, key)
        # Sentinel rather than {}: read_json swallows a corrupt or missing file,
        # and "zero positions" is a very different claim from "cannot read the
        # wallet of record" (engine_common.load_state raises for this reason).
        state = ec.read_json(os.path.join(_ROOT, "state", fname), None)
        if not isinstance(state, dict):
            alerts.append(f"{label}: state/{fname} missing or unreadable — "
                          f"its positions are NOT in this snapshot")
            continue
        age = _run_age(state.get("last_run"))
        if age is None:
            alerts.append(f"{label}: no last_run stamp — cannot tell when the "
                          f"engine last ran")
        elif age > STALE_RUN_SECONDS:
            alerts.append(f"{label}: engine has not run since "
                          f"{str(state.get('last_run'))[:16].replace('T', ' ')} "
                          f"({_ago(age)} ago) — prices below are at best that old, "
                          f"and TP/SL have gone unmanaged since")
        quotes = state.get("last_quotes") or {}
        for tk, pos in (state.get("positions") or {}).items():
            rows.append((key, tk, pos, quotes.get(tk) or {}, age))
    rows.sort(key=lambda r: str(r[2].get("entry_ts") or ""), reverse=True)
    return rows, alerts


def build(engine, ticker, pos, lq, run_age=None):
    label = notify.ENGINE_LABELS.get(engine, engine)
    entry = pos.get("entry_price") or 0
    shares = pos.get("shares") or 0
    now = lq.get("last") or entry
    ret = (now / entry - 1) if entry else 0
    pnl = (now - entry) * shares
    # 'stale' means the engine's last live fetch failed and it kept the previous
    # good price — the same warning the live pages put on the row.
    stale = " (stale quote)" if lq.get("source") == "stale" else ""
    # A dead engine is the louder of the two: source stays "live" forever, so
    # without this the row reads as a fresh mark. Said on the row itself
    # because each position is its own push — the header alert can be missed.
    if run_age is not None and run_age > STALE_RUN_SECONDS:
        stale = f" (engine last ran {_ago(run_age)} ago)"

    title = f"🧪 {label} holds {ticker}"
    lines = [
        f"{'+' if ret >= 0 else ''}{ret * 100:.2f}%  ·  {notify._sek(pnl)} unrealized",
        f"{notify._fmt(shares, 0)} sh  {notify._fmt(entry)} → {notify._fmt(now)}{stale}",
        f"TP {notify._fmt(pos.get('tp'))} / SL {notify._fmt(pos.get('sl'))}"
        f"  ·  exit by {pos.get('target_exit_date') or '—'}",
        f"Bought {str(pos.get('entry_ts') or pos.get('entry_date') or '')[:16].replace('T', ' ')}"
        f"  ·  held {_held_for(pos.get('entry_ts'))}",
    ]
    # Green when it is in profit, red when it is not — matching the sell pills.
    tag = "green_circle" if ret >= 0 else "red_circle"
    return title, lines, tag


def main():
    dry = "--dry-run" in sys.argv
    rows, alerts = collect()

    # Queued first so it lands above the position pushes on the phone.
    if alerts:
        notify.queue_message("🧪 ⚠️ Snapshot may not be the live book",
                             alerts, tag="warning")

    if not rows:
        # Still send something: "nothing is open" is a real answer, and a test
        # that produces silence is indistinguishable from a broken sink.
        body = ["All three wallets are flat right now."]
        if alerts:
            # Do not assert flatness we cannot prove — a wallet that could not
            # be read is not a wallet with no positions.
            body = ["No positions found, but see the warning above — this is "
                    "not a confirmed flat book."]
        notify.queue_message("🧪 No open positions", body, tag="zzz")
    for engine, tk, pos, lq, age in rows:
        title, lines, tag = build(engine, tk, pos, lq, age)
        notify.queue_message(title, lines, tag=tag)

    print(f"{len(rows)} open position(s) across the three wallets")
    for alert in alerts:
        print(f"  ! {alert}")
    for engine, tk, pos, lq, age in rows:
        title, lines, _ = build(engine, tk, pos, lq, age)
        print(f"\n  {title}")
        for line in lines:
            print(f"     {line}")

    st = notify.status()
    print(f"\nsinks: {', '.join(st['sinks']) or 'NONE'}"
          f"   (enabled={st['enabled']} macos={st['macos']} ntfy_ready={st['ntfy_ready']})")
    if dry:
        notify._queue.clear()        # drain, so the atexit flush cannot send
        print("--dry-run: nothing sent")
        return
    sent, failed, dropped = notify.flush()
    print(f"sent={sent} failed={failed} dropped={dropped}")


if __name__ == "__main__":
    main()
