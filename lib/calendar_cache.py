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

`refresh_cache()` adds `refresh_attempted_at` and `refresh_failed` (the insrefs
whose fetch failed on the last run, whose events are therefore carried over from
an earlier one). `fetched_at` only advances when *every* stock came back, so a
partial refresh stays stale and the next sweep retries it.

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

import engine_common as ec          # write_json_atomic — see save_cache()

ROOT       = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # project root (this module lives in lib/)
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
    """One-stock fetch from Millistream's calendar endpoint.

    Returns a list of normalised event dicts (sorted by date), [] when the
    stock genuinely has no events in the window, and None when the *fetch*
    failed (network error, timeout, token rejection, unparseable body).

    The two used to be the same value, and refresh_cache() stored the [] from a
    failed fetch as if it were an answer — erasing real ex-dividend/earnings
    dates and switching the entry gate off for those stocks. Callers that only
    display events can keep treating None as empty (`evts or []`)."""
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
        return None
    if not isinstance(data, list):
        # An error payload (token rejected, endpoint hiccup) comes back as a
        # dict, not the list-of-one the calendar returns. That is a failure,
        # not an empty calendar.
        return None
    if not data:
        return []
    return data[0].get("calendarevent", []) or []


def load_cache():
    """Return cache dict {fetched_at, events}, or an empty skeleton if absent.

    A corrupt or truncated file also yields a skeleton, but one carrying
    `load_error` — because an empty `events` map makes has_nearby_event()
    answer "nothing nearby" for *every* stock, i.e. the entry gate stops
    blocking, and that must not be indistinguishable from the normal case.
    cache_health() is what surfaces it."""
    if not os.path.exists(CACHE_PATH):
        return {"fetched_at": None, "events": {}}
    try:
        with open(CACHE_PATH) as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
        return {"fetched_at": None, "events": {},
                "load_error": "events_calendar.json is not a JSON object"}
    except (json.JSONDecodeError, OSError) as e:
        return {"fetched_at": None, "events": {}, "load_error": str(e)}


def save_cache(cache, stamp=True):
    """Persist the cache. `stamp=False` deliberately leaves `fetched_at`
    untouched: a refresh that could not reach every stock must not read as
    fresh, or is_stale() would keep the next sweep from retrying the ones it
    missed.

    The write goes through write_json_atomic() because this file used to be
    opened in "w" mode — 176 KB truncated in place and written back over many
    syscalls — while app.py's request threads and all three engines read it. A
    crash or an interleaved read landed on a torn file, which load_cache() then
    read as "no events anywhere"."""
    # `load_error` describes the *previous* read of this file, not the content
    # we are about to write. Left in, a single corrupt read would be baked into
    # the next good file and cache_health() would report the cache as untrusted
    # forever.
    cache.pop("load_error", None)
    if stamp:
        cache["fetched_at"] = datetime.now(timezone.utc).isoformat()
    ec.write_json_atomic(CACHE_PATH, cache, default=str)


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
    cache dict.

    A fetch that fails leaves that insref's previously cached events in place
    and is counted into `refresh_failed`; the cache is only stamped fresh when
    every stock came back. The old code assigned fetch_calendar()'s result
    unconditionally, so Wi-Fi dropping partway through the 17:52 sweep replaced
    the remaining stocks' real dividend/earnings dates with [] *and* marked the
    cache fresh for 24h — the ±7d entry gate then blocked nothing, with no skip
    row and no log line to show for it."""
    cache = load_cache()
    events_map = cache.get("events", {})
    if not isinstance(events_map, dict):
        events_map = {}
    total = len(insrefs)
    failed = []
    for i, ins in enumerate(insrefs, start=1):
        if progress:
            try: progress(i, total, ins)
            except Exception: pass
        evts = fetch_calendar(ins)
        if evts is None:
            # Could not fetch — keep whatever we already had for this stock.
            # Stale event dates still gate correctly; [] does not gate at all.
            failed.append(str(ins))
            continue
        # Strip eventlink (long uuid we don't need) but keep the rest
        events_map[str(ins)] = [
            {k: v for k, v in e.items() if k != "eventlink"}
            for e in evts
        ]
    cache["events"] = events_map
    cache["refresh_attempted_at"] = datetime.now(timezone.utc).isoformat()
    cache["refresh_failed"] = failed
    if failed:
        shown = ", ".join(failed[:5]) + ("…" if len(failed) > 5 else "")
        print(f"   ✗ event calendar: {len(failed)}/{total} fetches failed ({shown}) — "
              f"kept the previous events for those stocks and left the cache "
              f"stale so the next sweep retries")
    # stamp=not failed: a partial refresh must stay stale (see save_cache).
    save_cache(cache, stamp=not failed)
    return cache


def cache_health(cache=None):
    """Trustworthiness summary of the cache, for the run log and /live-health.

    has_nearby_event() fails *open* by design — an insref with no cached events
    is not blocked, which is what keeps stocks outside the refreshed universe
    tradeable at all. The cost is that an empty, unreadable or half-refreshed
    cache silently turns the entry gate off: nothing is blocked, so no skip row
    is ever logged and the gate keeps reading as "working". This is the only
    place that difference is visible, so it belongs in the UI."""
    if cache is None:
        cache = load_cache()
    events = cache.get("events") or {}
    failed = cache.get("refresh_failed") or []
    stale  = is_stale(cache)
    covered = sum(1 for v in events.values() if isinstance(v, list))
    return {
        "fetched_at":           cache.get("fetched_at"),
        "refresh_attempted_at": cache.get("refresh_attempted_at"),
        "stale":                stale,
        "covered":              covered,
        "with_events":          sum(1 for v in events.values() if v),
        "failed":               len(failed),
        "failed_insrefs":       failed[:20],
        "load_error":           cache.get("load_error"),
        "ok": not cache.get("load_error") and not failed and covered > 0 and not stale,
    }


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
        # Fails open on purpose: stocks outside the refreshed universe have no
        # cached events and must stay tradeable. It does mean a broken cache
        # reads exactly like "nothing scheduled" here — cache_health() is what
        # tells the two apart, so keep it on /live-health.
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
