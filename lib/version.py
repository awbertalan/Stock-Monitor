"""Read API over CHANGELOG.json — the project's version ledger.

Two tiers, deliberately:

  * **app_version** ("0.11.0") bumps on *every* Claude-assisted change. It is an
    audit trail: given a trade, which build of the code opened it.

  * **strategy_version** ("sig-5" / "pat-3" / "x-2") bumps *only* when an
    engine's tradeable parameters, gates or detectors change. It is the
    grouping key for win-rate / P&L analysis.

The distinction matters. Stamping a single global version on trades would put a
CSS tweak and the 2026-05-25 stop widening -- which moved break-even WR from
33.3% to 50.0% and made all three wallets -EV -- on the same footing, and
scatter the trade record across dozens of near-identical versions with n<=2
each. Grouping only ever wants the strategy tier.

Everything here fails soft. CHANGELOG.json is documentation; an engine must
never fail to trade because it is missing, unparseable or half-written. A
missing ledger yields ``None`` versions and an empty stamp, and the engines
carry on.
"""

import json
import os
import re
import threading

LEDGER_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "CHANGELOG.json")

ENGINES = ("signals", "patterns", "cross", "breakout")

_cache = {"mtime": None, "data": None}
_lock = threading.Lock()


def ledger(force=False):
    """The parsed ledger, re-read whenever the file's mtime changes.

    The engines call this on every run and app.py on every page load, so it is
    cached; but it is edited by hand between runs, so the cache is keyed on
    mtime rather than being read once at import."""
    with _lock:
        try:
            mtime = os.path.getmtime(LEDGER_PATH)
        except OSError:
            _cache["mtime"], _cache["data"] = None, None
            return None
        if force or _cache["data"] is None or _cache["mtime"] != mtime:
            try:
                with open(LEDGER_PATH, encoding="utf-8") as f:
                    _cache["data"] = json.load(f)
                _cache["mtime"] = mtime
            except (json.JSONDecodeError, OSError):
                # Keep serving the last good parse rather than blanking every
                # version pill mid-edit of the ledger.
                pass
        return _cache["data"]


def app_version():
    d = ledger()
    return (d or {}).get("app_version")


def strategy_version(engine):
    """Current strategy version for ``engine`` ('signals' | 'patterns' | 'cross' | 'breakout')."""
    d = ledger()
    return ((d or {}).get("strategy_versions") or {}).get(engine)


def stamp(engine):
    """The dict to merge into a newly-opened position.

    Returns ``{}`` when the ledger is unavailable, so ``pos.update(stamp(...))``
    is a no-op rather than writing null versions into the wallet of record."""
    app = app_version()
    strat = strategy_version(engine)
    if app is None and strat is None:
        return {}
    return {
        "app_version":      app,
        "strategy_version": strat,
        "version_source":   "live",
    }


def _version_seq(version):
    """Trailing number of a strategy version ('sig-12' -> 12). Orders versions
    that share a start date — sig-6, sig-7 and sig-8 all start 2026-08-24."""
    m = re.search(r"(\d+)$", str(version or ""))
    return int(m.group(1)) if m else 0


def strategy_history(engine):
    """Chronological [{version, from, params, note, app_version}, ...] for ``engine``.

    Sorted here rather than trusted from the file: the 2026-09-06 rows (sig-11,
    pat-5, x-5) were inserted at the head of each list, and strategy_version_at()
    stops at the first later row, so it returned None for every earlier date.
    A row with no 'from' sorts last, where it cannot cut that scan short."""
    d = ledger()
    rows = ((d or {}).get("strategy_history") or {}).get(engine) or []
    rows = [r for r in rows if isinstance(r, dict)]     # fail soft on a hand edit
    return sorted(rows, key=lambda r: (str(r.get("from") or "9999-99-99"),
                                       _version_seq(r.get("version"))))


def strategy_version_at(engine, date_str):
    """Which strategy version was active for ``engine`` on ``date_str`` (an ISO
    date). Used by scripts/backfill_versions.py to reconstruct the versions of
    trades opened before this ledger existed, and by the UI to explain a
    backfilled pill. Returns None for dates before the engine launched."""
    if not date_str:
        return None
    date_str = str(date_str)[:10]
    active = None
    for row in strategy_history(engine):
        if row.get("from") and row["from"] <= date_str:
            active = row.get("version")
        else:
            break          # history is chronological
    return active


def entry_for_version(app_ver):
    """The changelog entry that introduced ``app_ver``, or None."""
    for e in (ledger() or {}).get("entries") or []:
        if e.get("version") == app_ver:
            return e
    return None


def strategy_row(engine, version):
    """The strategy_history row for a given engine + strategy version, or None.
    Feeds the UI tooltip that explains what a version pill actually changed."""
    for row in strategy_history(engine):
        if row.get("version") == version:
            return row
    return None


def app_version_at(date_str):
    """The app version in effect on ``date_str`` — the newest entry dated at or
    before it. Used to backfill trades opened before the ledger existed."""
    if not date_str:
        return None
    date_str = str(date_str)[:10]
    for e in (ledger() or {}).get("entries") or []:   # newest-first
        if e.get("date") and e["date"] <= date_str:
            return e.get("version")
    return None
