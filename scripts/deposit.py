#!/usr/bin/env python3
"""Record a cash deposit into one live paper-trade wallet.

Third time this has been done by hand (Patterns 2026-08-24, Signals 2026-09-03,
Cross 2026-09-05), and each time it is the same three fields that have to move
together or the numbers quietly lie:

    cash          += amount     the money is actually spendable
    wallet_start  += amount     contributed capital, what return% is measured
                                against — see engine_common.wallet_start()
    deposits[]    += a row      the audit trail: when, how much, why

Bumping `cash` alone makes a top-up read as profit on every page at once
(equity/wallet_start is how all five consumers compute return). Bumping
`wallet_start` alone silently dilutes the recorded return of past trades.

Dry-run by default; --apply writes. Backs up to state/backups/ first, and
refuses to write while an engine run is in flight — write_json_atomic protects
against a torn write, not a lost update.

Also refuses a deposit that looks like one already in deposits[] (same date and
amount). This tool has no undo — negative amounts are refused by design, so the
only remedies for a double-credit are hand-editing the wallet or restoring the
pre_deposit backup, and that restore would roll back every engine trade booked
since. And a duplicate is worse than extra cash: wallet_start is the denominator
every return% on every page is measured against, so it corrupts the whole
performance record permanently. Recalling the line from shell history after an
ambiguous outcome is exactly how that happens, so the second run has to be the
one that stops. --id gives a genuinely exactly-once handle for the legitimate
case (two real top-ups of the same size on the same day); --force-duplicate is
the escape hatch when the collision is inspected and understood.

    python3 scripts/deposit.py cross 500000 --note "..."
    python3 scripts/deposit.py cross 500000 --note "..." --apply
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime

ROOT      = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_DIR = os.path.join(ROOT, "state")
BACKUPS   = os.path.join(STATE_DIR, "backups")
sys.path.insert(0, os.path.join(ROOT, "lib"))

import engine_common as ec   # noqa: E402

WALLETS = {
    "signals":  ("Signals",  "live_portfolio.json"),
    "patterns": ("Patterns", "live_patterns_portfolio.json"),
    "cross":    ("Cross",    "live_cross_portfolio.json"),
}


def engines_running():
    """Names of any live engine process currently mid-run. Same check as
    scripts/retighten_stops.py — an engine run landing between this script's
    read and its write would clobber the deposit wholesale."""
    try:
        ps = subprocess.run(["ps", "ax", "-o", "command"],
                            capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return ["<ps unavailable>"]      # cannot prove it is safe — treat as busy
    hits = []
    for line in ps.splitlines():
        if "deposit.py" in line:
            continue
        for name in ("live_signals.py", "live_patterns.py", "live_cross.py"):
            if name in line:
                hits.append(name)
    return sorted(set(hits))


def duplicate_rows(state, today, amount):
    """Rows in deposits[] that this run would be a repeat of: same date, same
    amount. Note text is deliberately NOT part of the match — an operator who
    re-runs the line from history reproduces the note verbatim, and one who
    retypes it with a comma moved has still made the same deposit twice."""
    out = []
    for i, row in enumerate(state.get("deposits") or []):
        if not isinstance(row, dict):
            continue
        try:
            same_amount = abs(float(row.get("amount") or 0.0) - amount) < 0.005
        except (TypeError, ValueError):
            continue
        if same_amount and str(row.get("date") or "") == today:
            out.append((i, row))
    return out


def row_with_id(state, deposit_id):
    """The already-recorded row carrying this --id, if any. A repeat id is the
    same deposit re-submitted, not a new one, so it is a no-op rather than a
    refusal — that is what makes retrying a run whose outcome was unclear safe."""
    for row in (state.get("deposits") or []):
        if isinstance(row, dict) and deposit_id and row.get("id") == deposit_id:
            return row
    return None


def main():
    ap = argparse.ArgumentParser(description="Record a deposit into a live wallet.")
    ap.add_argument("engine", choices=sorted(WALLETS))
    ap.add_argument("amount", type=float, help="SEK to add (positive)")
    ap.add_argument("--note", required=True, help="why the top-up was made")
    ap.add_argument("--apply", action="store_true", help="write (default is dry-run)")
    ap.add_argument("--id", dest="deposit_id", default=None,
                    help="stable id for this deposit; re-running with the same id "
                         "is a no-op instead of a second credit")
    ap.add_argument("--force-duplicate", action="store_true",
                    help="record even though deposits[] already holds a row with "
                         "this date and amount (inspect the printed row first)")
    args = ap.parse_args()

    if args.amount <= 0:
        print("REFUSED — amount must be positive. This tool does not withdraw.")
        return 1

    label, filename = WALLETS[args.engine]
    path = os.path.join(STATE_DIR, filename)
    state = ec.read_json(path, None)
    if state is None:
        print(f"REFUSED — {label} wallet not found at {path}")
        return 1

    today   = datetime.now().date().isoformat()

    # Idempotency, checked in dry-run too so the operator finds out before typing
    # --apply rather than after. There is no in-tool undo (see module docstring),
    # so a repeat has to be stopped on the way in.
    seen = row_with_id(state, args.deposit_id)
    if seen is not None:
        print(f"ALREADY RECORDED — {label} deposits[] holds id {args.deposit_id!r}:")
        print(f"    {json.dumps(seen, ensure_ascii=False)}")
        print("Nothing to do; this deposit is already credited.")
        return 0

    dupes = duplicate_rows(state, today, args.amount)
    if dupes and not args.force_duplicate:
        print(f"REFUSED — {label} deposits[] already holds {len(dupes)} row(s) dated "
              f"{today} for {args.amount:,.2f} SEK:")
        for i, row in dupes:
            print(f"    [{i}] {json.dumps(row, ensure_ascii=False)}")
        print("Nothing written. If this really is a second, separate top-up of the "
              "same size today,\nre-run with --force-duplicate (and preferably "
              "--id <something-unique> so a retry stays safe).")
        return 1

    old_cash  = float(state.get("cash") or 0.0)
    old_start = float(ec.wallet_start(state, 500_000.0))
    new_cash  = old_cash + args.amount
    new_start = old_start + args.amount

    open_val = sum((p.get("shares") or 0) * ((state.get("last_quotes", {}).get(tk) or {}).get("last")
                                             or p.get("entry_price") or 0)
                   for tk, p in (state.get("positions") or {}).items())

    print(f"{label} wallet — {path}")
    print(f"  cash          {old_cash:>12,.2f}  ->  {new_cash:>12,.2f}")
    print(f"  wallet_start  {old_start:>12,.2f}  ->  {new_start:>12,.2f}   (contributed capital)")
    print(f"  deposits[]    {len(state.get('deposits') or [])} row(s)  ->  "
          f"{len(state.get('deposits') or []) + 1}")
    print(f"  open positions {len(state.get('positions') or {})} worth {open_val:,.0f} SEK")
    eq_old = old_cash + open_val
    eq_new = new_cash + open_val
    r_old  = (eq_old / old_start - 1) * 100
    r_new  = (eq_new / new_start - 1) * 100
    print(f"  equity        {eq_old:>12,.2f}  ->  {eq_new:>12,.2f}")
    print(f"  return%       {r_old:>+11.2f}%  ->  {r_new:>+11.2f}%")
    # Expected, not a fault, but it should never be a surprise: the P&L in SEK
    # is untouched, so an existing gain or loss spread over a larger base moves
    # toward zero. The deposit is correctly not counted as profit — that is the
    # whole point of measuring against contributed capital — but the historical
    # return of trades already closed is diluted by money that never funded
    # them. Both prior top-ups (Patterns 2026-08-24, Signals 2026-09-03) did
    # the same thing. Judge a strategy on its per-trade record, not on this tile.
    if abs(r_new) < abs(r_old) - 1e-9:
        print(f"                (P&L unchanged at {eq_old - old_start:+,.0f} SEK — the "
              f"percentage moves toward zero because the base grew, not because\n"
              f"                 anything improved. Prior closed trades are diluted by "
              f"capital that never funded them.)")
    print(f"  note          {args.note}")
    if args.deposit_id:
        print(f"  id            {args.deposit_id}")
    if dupes:
        print(f"  !! FORCED DUPLICATE — {len(dupes)} matching row(s) already dated "
              f"{today} for {args.amount:,.2f} SEK will be left in place and this "
              f"one added on top.")

    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply.")
        return 0

    busy = engines_running()
    if busy:
        print(f"\nREFUSED — engine run in flight ({', '.join(busy)}). "
              f"Nothing written; re-run once it finishes.")
        return 1

    # Re-read after the guard so we act on the freshest state we can. The state
    # read at the top is stale by now; re-run both idempotency checks against
    # what is actually on disk, or a deposit recorded in the meantime (another
    # terminal, a run that did land after printing an unclear outcome) would be
    # silently doubled by this one. Both re-checks only read, and both run
    # BEFORE the backup is taken: a refusal here must not leave an orphan
    # pre_deposit file behind, because a file in state/backups/ is the marker
    # an operator reads as "a deposit landed here".
    state = ec.read_json(path, None)
    if state is None:
        print(f"\nREFUSED — {label} wallet vanished from {path}. Nothing written.")
        return 1
    seen = row_with_id(state, args.deposit_id)
    if seen is not None:
        print(f"\nALREADY RECORDED — id {args.deposit_id!r} appeared in {label} "
              f"deposits[] since this run started:")
        print(f"    {json.dumps(seen, ensure_ascii=False)}")
        print("Nothing written; this deposit is already credited.")
        return 0
    if duplicate_rows(state, today, args.amount) and not args.force_duplicate:
        print(f"\nREFUSED — a matching {today} row for {args.amount:,.2f} SEK "
              f"appeared in {label} deposits[] since this run started. "
              f"Nothing written.")
        return 1
    # How many matching rows exist before we write, so the read-back below can
    # tell our own row apart from ones that were already there (--force-duplicate).
    n_before = len(duplicate_rows(state, today, args.amount))

    os.makedirs(BACKUPS, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = os.path.join(BACKUPS, f"{filename.rsplit('.', 1)[0]}.pre_deposit.{stamp}.json")
    shutil.copy2(path, backup)

    row = {"date": today, "amount": args.amount, "note": args.note}
    if args.deposit_id:
        row["id"] = args.deposit_id
    state["cash"] = float(state.get("cash") or 0.0) + args.amount
    state["wallet_start"] = float(ec.wallet_start(state, 500_000.0)) + args.amount
    state.setdefault("deposits", [])
    state["deposits"].append(row)
    ec.write_json_atomic(path, state, ensure_ascii=False)

    # write_json_atomic protects against a torn write, not a lost update, and
    # engines_running() only proves no engine was mid-run at check time. A tick
    # that started in the gap read the pre-deposit wallet and will save_state()
    # its whole copy over us seconds later, taking cash, wallet_start and the
    # row with it. Read the file back so that lands as a loud failure instead of
    # a "Written." that is not true. Only the row's presence is compared: cash
    # legitimately moves if an engine books an entry or exit after our write,
    # and a false "lost" would push the operator into re-running --apply, which
    # is the double-credit this tool exists to prevent.
    fresh = ec.read_json(path, None) or {}
    if args.deposit_id:
        landed = row_with_id(fresh, args.deposit_id) is not None
    else:
        landed = len(duplicate_rows(fresh, today, args.amount)) > n_before
    if not landed:
        print(f"\nDEPOSIT LOST — the write went out but {path} no longer holds the "
              f"row;\nan engine run that started before the guard has overwritten it. "
              f"The pre-deposit\nbackup is at {backup}. Inspect the wallet and re-run "
              f"only once no engine is in flight —\ndo NOT re-run blind, deposits[] is "
              f"the only record of what was credited.")
        return 1

    print(f"\nWritten. Backup at {backup}")
    print(f"  cash {float(fresh.get('cash') or 0.0):,.2f}   contributed "
          f"{float(ec.wallet_start(fresh, 500_000.0)):,.2f}   (re-read from disk)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
