"""
Corporate-event calendar cache.

The Millistream `cmd=calendar` endpoint exposes dividend ex-dates (type=0) and
earnings/reports (type=15) per insref. We cache the result in
`events_calendar.json` and let the trading engines query it before opening a
position — if the candidate has any event within the configured window we
skip the entry. This avoids:
  - Buying at the stale pre-ex-dividend price right before the gap (Tele2 A
    on 2026-05-25 was the trigger — entered at 195 SEK against a 5.25 SEK
    dividend going ex on 2026-05-19, immediately stopped out).
  - Entering into earnings-announcement volatility (gap risk in either
    direction, neither of which the validated mean-reversion or
    candlestick-pattern setups are designed for).

Cache shape (events_calendar.json):
    {
      "fetched_at": "2026-05-25T18:30:00+00:00",
      "events": {
        "<insref>": [
          {"date": "2026-05-19", "type": 0,  "subtype": 4, "dividend": 5.25,
           "paymentdate": "2026-05-25", "period": null},
          {"date": "2026-04-23", "type": 15, "subtype": 0, ..., "period": "2026-Q1"},
          ...
        ],
        ...
      }
    }

The engines call `has_nearby_event(insref, today, window=7)` which returns
(blocked: bool, reason: str|None). `reason` is a short human string suitable
for log lines + UI skip-row display.
"""

import json
import os
from datetime import date, datetime, timezone, timedelta
from urllib.request import urlopen

import env_loader
env_loader.load_env()

ROOT       = os.path.dirname(os.path.abspath(__file__))
STATE_DIR  = os.path.join(ROOT, "state")
CACHE_PATH = os.path.join(STATE_DIR, "events_calendar.json")
TOKEN      = os.environ["MILLISTREAM_TOKEN"]
_BASE      = "https://mws-2.millistream.com/mws.fcgi"

# Default skip window — entries blocked if any event date falls within
# [today - WINDOW_DAYS, today + WINDOW_DAYS]. Symmetric ±7 calendar days
# catches the Tele2 A pattern (ex-div was 6 days before entry) and gives
# earnings volatility a few days to settle.
WINDOW_DAYS = 7

# Cache is considered stale after this many hours — refresh from EOD sweep.
STALE_AFTER_HOURS = 24

# Event types we pull (Millistream MDF_RC mapping): 0 = dividend, 15 = calendar
_EVENT_TYPES = "0,15"


def _parse_date(s):
    """Parse YYYY-MM-DD → date, returns None on bad input."""
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def fetch_calendar(insref, days_back=180, days_fwd=180, timeout=8):
    """One-stock fetch from Millistream's calendar endpoint. Returns a list
    of normalised event dicts (sorted by date) or [] on any failure."""
    start = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    end   = (datetime.now() + timedelta(days=days_fwd)).strftime("%Y-%m-%d")
    url = (
        f"{_BASE}?cmd=calendar"
        f"&fields=date,subtype,type,eventlink,dividend,paymentdate,period"
        f"&filetype=json&token={TOKEN}&insref={insref}"
        f"&orderby=date&order=asc&type={_EVENT_TYPES}"
        f"&startdate={start}&enddate={end}&limit=60"
    )
    try:
        raw = urlopen(url, timeout=timeout).read().decode("utf-8")
        data = json.loads(raw)
    except Exception:
        return []
    if not isinstance(data, list) or not data:
        return []
    return data[0].get("calendarevent", []) or []


def load_cache():
    """Return cache dict {fetched_at, events}, or an empty skeleton if absent."""
    if not os.path.exists(CACHE_PATH):
        return {"fetched_at": None, "events": {}}
    try:
        with open(CACHE_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {"fetched_at": None, "events": {}}
    except (json.JSONDecodeError, OSError):
        return {"fetched_at": None, "events": {}}


def save_cache(cache):
    cache["fetched_at"] = datetime.now(timezone.utc).isoformat()
    with open(CACHE_PATH, "w") as f:
        json.dump(cache, f, indent=2, default=str)


def is_stale(cache, hours=STALE_AFTER_HOURS):
    fa = cache.get("fetched_at")
    if not fa:
        return True
    try:
        last = datetime.fromisoformat(fa)
    except ValueError:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - last).total_seconds() > hours * 3600


def refresh_cache(insrefs, progress=None):
    """Re-pull every insref in `insrefs` and persist. `progress` (optional)
    is called as progress(idx, total, name) for UI updates. Returns the new
    cache dict."""
    cache = load_cache()
    events_map = cache.get("events", {})
    if not isinstance(events_map, dict):
        events_map = {}
    total = len(insrefs)
    for i, ins in enumerate(insrefs, start=1):
        if progress:
            try: progress(i, total, ins)
            except Exception: pass
        evts = fetch_calendar(ins)
        # Strip eventlink (long uuid we don't need) but keep the rest
        events_map[str(ins)] = [
            {k: v for k, v in e.items() if k != "eventlink"}
            for e in evts
        ]
    cache["events"] = events_map
    save_cache(cache)
    return cache


def has_nearby_event(insref, today, window_days=WINDOW_DAYS, cache=None):
    """Return (blocked: bool, reason: str|None).

    Blocked iff any cached event for `insref` has a date within
    [today - window_days, today + window_days] (inclusive). The reason
    string is a compact human description like "div +5.25 ex 2026-05-19
    (6d ago)" or "Q1 report 2026-04-23 (+3d)" for log/UI display."""
    if cache is None:
        cache = load_cache()
    events = (cache.get("events") or {}).get(str(insref), [])
    if not events:
        return False, None
    if isinstance(today, datetime):
        today = today.date()
    win_start = today - timedelta(days=window_days)
    win_end   = today + timedelta(days=window_days)
    best = None
    for e in events:
        d = _parse_date(e.get("date"))
        if d is None: continue
        if d < win_start or d > win_end: continue
        # Prefer the event closest to today
        gap = (d - today).days
        if best is None or abs(gap) < abs(best[0]):
            best = (gap, d, e)
    if best is None:
        return False, None
    gap, d, e = best
    sign = f"+{gap}d" if gap > 0 else (f"{gap}d ago" if gap < 0 else "today")
    if e.get("type") == 0:
        amt = e.get("dividend")
        amt_str = f" {amt:+g} SEK" if isinstance(amt, (int, float)) else ""
        return True, f"dividend ex {d}{amt_str} ({sign})"
    period = e.get("period") or "report"
    return True, f"{period} report {d} ({sign})"
