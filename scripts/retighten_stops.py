#!/usr/bin/env python3
"""Bring already-open positions onto their engine's *current* SL_PCT.

Stops are stored per-position at entry, so changing an engine's SL_PCT never
reaches a trade that is already open. On 2026-08-24 live_signals' stop was
restored -2.0% -> -1.0% between two runs: seven positions filled at 13:04 kept a
-2% stop while the 15:11 fill got -1%, leaving one wallet running two risk
parameters at once — and the -2% half was the losing half.

The script holds no parameters of its own. Each engine's target comes from its
own module constant, so this only ever acts where a wallet has drifted from the
code it is running under. Patterns and Cross deliberately still run -2%, so
they show no work until their own SL_PCT changes.

Two safety properties matter more than the edit itself:

  * **Tighten only.** A stop is only ever raised (toward entry). If a position
    already carries a stop at or tighter than the target, it is left alone. The
    script can never widen risk.

  * **No synthetic fills.** A stop retightened *below where the market already
    is* would, on the next engine run, book an exit at the stop price — a
    better-than-market fill that never existed. Those positions are marked
    `sl_fill_at_market`, which live_signals.apply_exit_checks honours by filling
    at the real market price instead. The marker clears itself as soon as the
    position trades back above the new stop.

    So this does not force an exit. It sets the correct stop and lets the next
    session decide: recover above it and the position lives on with a proper
    stop; stay below and it exits at an honest price.

    python3 scripts/retighten_stops.py           # dry run, prints the plan
    python3 scripts/retighten_stops.py --apply   # back up, then write
"""

import os
import subprocess
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))
sys.path.insert(0, ROOT)

import engine_common as ec      # noqa: E402
import live_signals             # noqa: E402
import live_patterns            # noqa: E402
import live_cross               # noqa: E402

STATE_DIR  = os.path.join(ROOT, "state")
BACKUP_DIR = os.path.join(STATE_DIR, "backups")

# (engine_key, label, wallet file, module holding the live SL_PCT)
ENGINES = [
    ("signals",  "Signals",  "live_portfolio.json",          live_signals),
    ("patterns", "Patterns", "live_patterns_portfolio.json",  live_patterns),
    ("cross",    "Cross",    "live_cross_portfolio.json",     live_cross),
]


def engines_running():
    """Names of any live engine process currently mid-run.

    write_json_atomic protects against a torn write, not a lost update: an
    engine run landing between this script's read and its write would be
    clobbered wholesale. Refuse to write while one is in flight.
    """
    try:
        ps = subprocess.run(["ps", "ax", "-o", "command"],
                            capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return ["<ps unavailable>"]      # cannot prove it is safe — treat as busy
    hits = []
    for line in ps.splitlines():
        if "retighten_stops.py" in line:
            continue
        for name in ("live_signals.py", "live_patterns.py", "live_cross.py"):
            if name in line:
                hits.append(name)
    return sorted(set(hits))


def plan_for(state, sl_pct):
    """[(ticker, old_sl, new_sl, mark, through)] for positions needing a change.

    `through` is True when the current mark already sits at/below the new stop,
    i.e. the position needs the market-fill marker.
    """
    rows = []
    quotes = state.get("last_quotes") or {}
    for tk, pos in (state.get("positions") or {}).items():
        entry = pos.get("entry_price")
        old   = pos.get("sl")
        if not entry or old is None:
            continue
        new = round(entry * (1 + sl_pct), 4)
        if new <= old + 1e-9:
            continue                     # already at or tighter than target
        q = quotes.get(tk) or {}
        mark = q.get("last") if isinstance(q, dict) else None
        rows.append((tk, old, new, mark, mark is not None and mark <= new))
    return rows


def main():
    apply    = "--apply" in sys.argv
    stamp_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    now_iso  = datetime.now().replace(microsecond=0).isoformat()
    total    = 0

    # Refuse *before* touching anything — the check is worthless after the write.
    if apply:
        busy = engines_running()
        if busy:
            print(f"REFUSED — engine run in flight ({', '.join(busy)}). "
                  f"Nothing written; re-run once it finishes.")
            return 1

    for key, label, wallet, mod in ENGINES:
        path  = os.path.join(STATE_DIR, wallet)
        state = ec.read_json(path, None)
        if state is None:
            print(f"{label:<9} wallet not found at {path} — skipped")
            continue

        sl_pct = mod.SL_PCT
        rows   = plan_for(state, sl_pct)
        print(f"\n{label:<9} target SL {sl_pct * 100:+.2f}%   "
              f"{len(state.get('positions') or {})} open, {len(rows)} to retighten")
        if not rows:
            continue

        for tk, old, new, mark, through in rows:
            entry = state["positions"][tk]["entry_price"]
            note  = (f"mark {mark:.2f} already through -> fill at market"
                     if through else
                     (f"mark {mark:.2f} above stop" if mark is not None else "no mark"))
            print(f"   {tk:<24} entry {entry:>9.2f}   "
                  f"SL {old:>9.4f} -> {new:>9.4f} "
                  f"({(old / entry - 1) * 100:+.2f}% -> {(new / entry - 1) * 100:+.2f}%)   {note}")
            if apply:
                pos = state["positions"][tk]
                pos["sl_retightened"] = {
                    "from": old, "to": new, "at": now_iso,
                    "sl_pct": sl_pct,
                    "reason": f"{label} SL_PCT now {sl_pct * 100:+.2f}%; "
                              f"position opened under a looser stop",
                }
                pos["sl"] = new
                if through:
                    pos["sl_fill_at_market"] = True
                else:
                    pos.pop("sl_fill_at_market", None)
        total += len(rows)

        if apply and rows:
            backup = os.path.join(
                BACKUP_DIR, f"{wallet}.pre_retighten_stops.{stamp_ts}.json")
            os.makedirs(BACKUP_DIR, exist_ok=True)
            ec.write_json_atomic(backup, ec.read_json(path, {}), default=str)
            ec.write_json_atomic(path, state, default=str)

    print()
    if not apply:
        print(f"DRY RUN — {total} position(s) would be retightened. "
              f"Re-run with --apply to write (wallets are backed up first).")
        return 0
    print(f"Applied to {total} position(s). "
          f"Backups: {BACKUP_DIR}/*.pre_retighten_stops.{stamp_ts}.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
