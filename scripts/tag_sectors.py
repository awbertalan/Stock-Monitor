#!/usr/bin/env python3
"""Backfill the sector tag (columns 4,5) on every row of stock_names.csv.

Added 2026-08-31 after a session where 16 of the 17 real-estate names in the
158-name tradable universe fell together (mean -1.71% against -0.63% for
everything else) and four of the day's six exits were property, accounting for
75% of the loss. Nothing in the project stored a sector: `sectorl3name` was
fetched per-quote for the web UI and thrown away, and the scraper's
`Instrumenttype/Classification Sector/` tree is empty, so no engine or page
could see the concentration building.

Which field: the numeric `sector` / `sectorname` pair, not sectorl3name. L3 is
too granular to group a sell-off — Fabege reads "Kommersiella Fastigheter",
Wihlborgs "Kontor" and Pandox "Hotell & Resort", which would have looked like
three unrelated sectors on 2026-08-31. `sectorname` puts all three in
"Fastigheter". The numeric id is stored alongside because the name is
language-dependent (the feed is queried with lang=sv).

Reads and writes nothing but stock_names.csv, via webScrapper.upsert_sectors_bulk
so the shared _names_lock is honoured and the other four columns are preserved.
Safe to re-run: rows already carrying the same tag are skipped.

    python3 scripts/tag_sectors.py [--limit N] [--dry-run]
"""
import csv, json, os, sys, time
from urllib.request import urlopen
from urllib.error import URLError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))

import env_loader                                    # noqa: E402
env_loader.load_env()
import webScrapper as ws                             # noqa: E402

TOKEN = os.environ["MILLISTREAM_TOKEN"]
BASE  = "https://mws-2.millistream.com/mws.fcgi"

# The endpoint silently truncates its reply at ~248 rows however many insrefs are
# asked for, so batches stay well under that; 200 returned all 200 in testing.
BATCH      = 200
MAX_RETRY  = 3
NAMES_CSV  = os.path.join(ROOT, "stock_names.csv")


def fetch_batch(insrefs):
    """[(insref, sector_id, sector_name)] for one batch. [] on failure."""
    url = (f"{BASE}?cmd=quote&fields=insref,sector,sectorname"
           f"&lang=sv&filetype=json&token={TOKEN}&insref={','.join(insrefs)}")
    for attempt in range(MAX_RETRY):
        try:
            raw = urlopen(url, timeout=30).read().decode("utf-8")
            data = json.loads(raw) or []
            out = []
            for r in data:
                sname = (r.get("sectorname") or "").strip()
                if not sname:
                    continue          # funds, warrants, derivatives — no sector
                out.append((r.get("insref"), r.get("sector") or "", sname))
            return out
        except (URLError, ValueError, OSError) as e:
            if attempt == MAX_RETRY - 1:
                print(f"    batch failed after {MAX_RETRY} tries: {e!r}")
                return []
            time.sleep(1.5 * (attempt + 1))
    return []


def main():
    limit  = None
    dry    = "--dry-run" in sys.argv
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])

    insrefs = []
    with open(NAMES_CSV, newline="", encoding="utf-8") as f:
        for row in csv.reader(f):
            if len(row) >= 2 and row[0].strip().isdigit():
                insrefs.append(row[0].strip())
    if limit:
        insrefs = insrefs[:limit]

    total   = len(insrefs)
    batches = (total + BATCH - 1) // BATCH
    print(f"Tagging {total:,} rows in {batches} batches of {BATCH}"
          f"{'  [DRY RUN — nothing written]' if dry else ''}")

    # Every fetched row is accumulated and written in ONE upsert at the end.
    # _write_names_csv rewrites all ~59k rows (~2 MB) per call, so writing per
    # batch would mean 295 full rewrites — the same per-stock rewrite cost that
    # upsert_names_bulk was introduced to remove from the refresh sweep.
    tagged = written = 0
    pending = []
    t0 = time.time()
    for i in range(0, total, BATCH):
        chunk = insrefs[i:i + BATCH]
        rows  = fetch_batch(chunk)
        tagged += len(rows)
        pending.extend(rows)
        n = i // BATCH + 1
        if n % 25 == 0 or n == batches:
            print(f"  batch {n:>3}/{batches}  {tagged:,} tagged  "
                  f"{time.time() - t0:.0f}s")
    if pending and not dry:
        print(f"  writing {len(pending):,} tags in one pass ...")
        written = ws.upsert_sectors_bulk(pending)

    print(f"\nDone in {time.time() - t0:.0f}s — {tagged:,} of {total:,} rows have a "
          f"sector ({tagged / total * 100:.0f}%), {written:,} written.")
    if not dry:
        counts = {}
        with open(NAMES_CSV, newline="", encoding="utf-8") as f:
            for row in csv.reader(f):
                if len(row) >= 6 and row[5].strip():
                    counts[row[5].strip()] = counts.get(row[5].strip(), 0) + 1
        print(f"\n{len(counts)} distinct sectors. Top 12:")
        for s, c in sorted(counts.items(), key=lambda x: -x[1])[:12]:
            print(f"  {c:>6,}  {s}")


if __name__ == "__main__":
    main()
