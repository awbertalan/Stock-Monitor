#!/usr/bin/env python3
"""Reconstruct app/strategy versions for trades opened before CHANGELOG.json existed.

The three wallets hold ~100 closed trades with no version stamp. Rather than
starting the "By version" analysis from n=0, we infer each trade's strategy
version from its entry_date against the dates in CHANGELOG.json's
`strategy_history` -- so the ledger stays the single source of truth and this
script holds no dates of its own.

Inferred stamps are flagged `version_source="backfilled"` and the UI renders
them differently: the boundaries come from session notes written after the fact,
so a trade entered *on* a boundary date could belong to either side. Live stamps
(written by the engine at entry) are never overwritten.

    python3 scripts/backfill_versions.py           # dry run, prints the plan
    python3 scripts/backfill_versions.py --apply   # back up, then write
"""

import os
import sys
from collections import Counter
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))

import engine_common as ec      # noqa: E402
import version as V             # noqa: E402

STATE_DIR   = os.path.join(ROOT, "state")
BACKUP_DIR  = os.path.join(STATE_DIR, "backups")


def wallets():
    """[(engine_key, label, path), ...] straight from the ledger."""
    led = V.ledger() or {}
    out = []
    for key, meta in (led.get("engines") or {}).items():
        out.append((key, meta.get("label", key),
                    os.path.join(STATE_DIR, meta["wallet"])))
    return out


def backfill_record(engine, rec):
    """Stamp one position/trade dict. Returns the version applied, or None if it
    was already stamped or its entry date predates the engine's history."""
    if rec.get("strategy_version"):
        return None                      # live stamp or an earlier backfill run
    entry_date = rec.get("entry_date") or rec.get("entry_ts")
    sv = V.strategy_version_at(engine, entry_date)
    if not sv:
        return None
    rec["strategy_version"] = sv
    rec["app_version"]      = V.app_version_at(entry_date)
    rec["version_source"]   = "backfilled"
    return sv


def main():
    apply = "--apply" in sys.argv
    stamp_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    os.makedirs(BACKUP_DIR, exist_ok=True)
    grand = 0

    for engine, label, path in wallets():
        state = ec.read_json(path, None)
        if state is None:
            print(f"{label:<14} wallet not found at {path} — skipped")
            continue

        applied = Counter()
        skipped = 0
        for rec in list((state.get("positions") or {}).values()) + \
                   list(state.get("closed_trades") or []):
            sv = backfill_record(engine, rec)
            if sv:
                applied[sv] += 1
            else:
                skipped += 1

        total = sum(applied.values())
        grand += total
        breakdown = "  ".join(f"{k}:{n}" for k, n in sorted(applied.items())) or "—"
        print(f"{label:<14} stamp {total:>3}   keep {skipped:>3}   {breakdown}")

        if apply and total:
            backup = os.path.join(
                BACKUP_DIR,
                f"{os.path.basename(path)}.pre_version_backfill.{stamp_ts}.json")
            ec.write_json_atomic(backup, ec.read_json(path, {}), default=str)
            ec.write_json_atomic(path, state, default=str)

    if not apply:
        print(f"\nDRY RUN — {grand} record(s) would be stamped. "
              f"Re-run with --apply to write (wallets are backed up first).")
    else:
        print(f"\nApplied to {grand} record(s). "
              f"Backups: {BACKUP_DIR}/*.pre_version_backfill.{stamp_ts}.json")


if __name__ == "__main__":
    main()
