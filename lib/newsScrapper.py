from urllib.request import urlopen
from html.parser import HTMLParser
from datetime import date, datetime, timedelta, timezone
import json, re, os, time
import env_loader
env_loader.load_env()

try:
    from zoneinfo import ZoneInfo
    _STHLM = ZoneInfo("Europe/Stockholm")
except Exception:      # Python < 3.9 or no tz database: fall back to the EU rule
    _STHLM = None

TOKEN = os.environ["MILLISTREAM_TOKEN"]
_BASE = "https://mws-2.millistream.com/mws.fcgi"

# Checked against the live feed on 2026-10-25. Type 1 is Finwire's newswire
# (market colour, insider trades, short-seller disclosures) and type 4 is
# interim/quarterly reports — they were labelled "Regulatory" and "Other".
_NEWSTYPE_LABELS = {
    0: "Analyst",
    1: "Newswire",
    2: "Press Release",
    3: "Calendar",
    4: "Report",
}

_COMPANY_CACHE = {}   # instrument insref -> (company id, isin)


class _StripTags(HTMLParser):
    def __init__(self):
        super().__init__()
        self._parts = []

    def handle_data(self, data):
        self._parts.append(data)

    def get_text(self):
        return " ".join(p.strip() for p in self._parts if p.strip())


def _parse_body(xml_text):
    """Extract clean plain text from the <body> section of a NewsItem XML blob."""
    m = re.search(r"<body>(.*?)</body>", xml_text or "", re.DOTALL)
    if not m:
        return ""
    body = m.group(1)
    body = re.sub(r"<br\s*/?>", " ", body)
    body = re.sub(r"</p>", "\n", body, flags=re.IGNORECASE)
    parser = _StripTags()
    parser.feed(body)
    text = parser.get_text()
    text = re.sub(r" {2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _type_of(item):
    try:
        return int(item.get("newstype"))
    except (TypeError, ValueError):
        return None


# ── Time handling ────────────────────────────────────────────────────────────
# Requests carry no timezone parameter, so the API answers in UTC. Its own
# timezone=Europe/Stockholm conversion applies the standard offset only (UTC+1
# all year): every summer item came back an hour early, e.g. a 17:37 close
# report stamped 16:37. Everything here works in naive UTC and converts at the
# edges.

def _last_sunday(year, month):
    d = (date(year, month + 1, 1) - timedelta(days=1)) if month < 12 else date(year, 12, 31)
    return d - timedelta(days=(d.weekday() + 1) % 7)


def _eu_summer(utc_naive):
    """EU summer time: 01:00 UTC on the last Sunday of March until 01:00 UTC on
    the last Sunday of October."""
    y = utc_naive.year
    start = datetime.combine(_last_sunday(y, 3), datetime.min.time()) + timedelta(hours=1)
    end = datetime.combine(_last_sunday(y, 10), datetime.min.time()) + timedelta(hours=1)
    return start <= utc_naive < end


def _utc_to_local(u):
    """Naive UTC -> naive Stockholm wall clock."""
    if _STHLM is not None:
        return u.replace(tzinfo=timezone.utc).astimezone(_STHLM).replace(tzinfo=None)
    return u + timedelta(hours=2 if _eu_summer(u) else 1)


def _local_to_utc(local):
    """Naive Stockholm wall clock -> naive UTC."""
    if _STHLM is not None:
        return local.replace(tzinfo=_STHLM).astimezone(timezone.utc).replace(tzinfo=None)
    u = local - timedelta(hours=2)
    return u if _eu_summer(u) else local - timedelta(hours=1)


def _item_utc(item):
    try:
        return datetime.strptime(f"{item.get('date')} {(item.get('time') or '')[:8]}",
                                 "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


# ── Fetching ─────────────────────────────────────────────────────────────────

def _company_for(insref, timeout=10):
    """(company id, isin) for an instrument insref via cmd=quote, or None when the
    lookup itself failed (not cached, so the next call retries).

    News is keyed by the issuer, not the instrument: the news endpoint's own
    insref= filter matches the news SOURCE (Cision is 8195), so passing a stock's
    insref returned [] for every stock and kept the stock page's news panel empty."""
    key = str(insref)
    if key in _COMPANY_CACHE:
        return _COMPANY_CACHE[key]
    url = (f"{_BASE}?cmd=quote&insref={insref}&fields=insref,company,isin"
           f"&filetype=json&token={TOKEN}")
    try:
        data = json.loads(urlopen(url, timeout=timeout).read().decode("utf-8"))
    except Exception:
        return None
    if not isinstance(data, list) or not data:
        return None       # [] may be the API's intermittent empty answer: don't cache it
    row = data[0]
    out = (row.get("company"), (row.get("isin") or "").strip() or None)
    _COMPANY_CACHE[key] = out
    return out


def _query(params, timeout=10, tries=2):
    """One cmd=news call; a list, or None when every attempt failed.

    The endpoint sometimes answers a valid query with an empty list instead of
    an error (the market-wide pull lost nine whole days that way on
    2026-10-25), so an empty answer is retried before it is believed."""
    url = f"{_BASE}?cmd=news&{params}&filetype=json&token={TOKEN}"
    result = None
    for attempt in range(tries):
        try:
            data = json.loads(urlopen(url, timeout=timeout).read().decode("utf-8"))
        except Exception:
            data = None
        if isinstance(data, list):
            if data:
                return data
            result = []
        if attempt + 1 < tries:
            time.sleep(0.4)
    return result


def _twin_key(item):
    # Bilingual releases arrive twice (one newsid per language) from the same
    # distributor, in the same second, with the same type.
    return (item.get("name"), item.get("date"), item.get("time"), _type_of(item))


def _company_news(insref, startdate, limit, timeout=10, enddate=None, with_text=True):
    """News for an instrument's issuer, Swedish and English, as
    {"items": [...], "failed": [languages whose query failed]} or
    {"error": ..., "outage": bool}.

    lang= filters by language, and the old lang=sv dropped every English-only
    release; the English twin of a Swedish item is dropped. `failed` lets a caller
    that must not mistake an outage for "no news" (the shadow label) tell them
    apart. The company id is tried first, then the ISIN."""
    ids = _company_for(insref, timeout=timeout)
    if ids is None:
        return {"error": "company lookup failed (cmd=quote)", "outage": True}
    company, isin = ids
    selectors = ([f"company={company}"] if company else []) + ([f"isin={isin}"] if isin else [])
    if not selectors:
        return {"error": "no company id or ISIN for this instrument", "outage": False}
    fields = "newsid,newstype,date,time,headline,symbol,name" + (",text" if with_text else "")
    span = f"&startdate={startdate}" + (f"&enddate={enddate}" if enddate else "")
    last, down = None, []
    for sel in selectors:
        base = f"fields={fields}&limit={limit}{span}&{sel}"
        got = {lang: _query(base + f"&lang={lang}", timeout=timeout) for lang in ("sv", "en")}
        failed = [lang for lang, v in got.items() if v is None]
        if len(failed) == len(got):
            down += failed          # this selector is down; try the next one
            continue
        sv, en = got["sv"] or [], got["en"] or []
        seen = {_twin_key(x) for x in sv}
        last = {"items": sv + [x for x in en if _twin_key(x) not in seen], "failed": failed}
        if last["items"] or failed:
            return last
    if last is None:
        return {"error": "news fetch failed", "outage": True}
    # An empty answer from the fallback after the primary selector failed
    # outright is not a clean "no news".
    last["failed"] = sorted(set(down))
    return last


def fetch_news(insref, limit=20, days_back=90):
    """
    Returns a list of news items for the given instrument insref (newest first):
    [{newsid, newstype, newstype_label, date, time, headline, source, body}]
    date/time are Stockholm local. Returns {"error": ...} on failure or an empty
    list if no news was found. A partial fetch (one language failed) still
    returns what came back.
    """
    startdate = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%d")
    res = _company_news(insref, startdate, limit)
    if "error" in res:
        return {"error": res["error"]}

    rows = []
    for item in res["items"]:
        u = _item_utc(item)
        local = _utc_to_local(u) if u else None
        ntype = _type_of(item)
        rows.append((u or datetime.min, {
            "newsid":         item.get("newsid"),
            "newstype":       ntype,
            "newstype_label": _NEWSTYPE_LABELS.get(ntype, "News"),
            "date":           local.strftime("%Y-%m-%d") if local else item.get("date", ""),
            "time":           local.strftime("%H:%M") if local else (item.get("time") or "")[:5],
            "headline":       item.get("headline", ""),
            "source":         item.get("name") or item.get("symbol", ""),
            "body":           _parse_body(item.get("text", "")),
        }))
    rows.sort(key=lambda r: r[0], reverse=True)
    return [r for _, r in rows[:limit]]


def news_window_summary(insref, start_local, end_local, max_headlines=5, timeout=5):
    """Company news published in [start_local, end_local) — naive Stockholm
    wall-clock datetimes — as {n, by_type, headlines}. Used for the Live Signals
    shadow news label, so a failed or half-failed fetch is reported as
    {"error", "outage"} rather than counted as "no news". Never raises."""
    try:
        start_u, end_u = _local_to_utc(start_local), _local_to_utc(end_local)
        res = _company_news(insref, start_u.strftime("%Y-%m-%d"), 50, timeout=timeout,
                            enddate=end_u.strftime("%Y-%m-%d"), with_text=False)
        if "error" in res:
            return {"error": res["error"], "outage": res.get("outage", True)}
        if res["failed"]:
            return {"error": "news query failed for " + "/".join(res["failed"]), "outage": True}
        hits = []
        for item in res["items"]:
            u = _item_utc(item)
            if u is not None and start_u <= u < end_u:
                hits.append((u, item))
        hits.sort(key=lambda p: p[0])
        by_type = {}
        for _, item in hits:
            label = _NEWSTYPE_LABELS.get(_type_of(item), "News")
            by_type[label] = by_type.get(label, 0) + 1
        return {
            "n":         len(hits),
            "by_type":   by_type,
            "headlines": [f"{_utc_to_local(u).strftime('%H:%M')} {(item.get('headline') or '')[:140]}"
                          for u, item in hits[:max_headlines]],
        }
    except Exception as e:
        return {"error": str(e)[:200], "outage": True}
