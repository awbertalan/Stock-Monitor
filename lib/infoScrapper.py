from urllib.request import urlopen
from datetime import datetime, timezone, timedelta
import json, csv, os
import env_loader
env_loader.load_env()

TOKEN = os.environ["MILLISTREAM_TOKEN"]
_BASE = "https://mws-2.millistream.com/mws.fcgi"

_HISTORY_EPOCH  = "1970-01-01"   # startdate that means "everything"
_OVERLAP_DAYS   = 21             # how far back an incremental history pull asks for;
                                 # the endpoint clamps to a ~2-month floor regardless
_TAIL_BYTES     = 8192           # ~250 daily rows — comfortably covers that window
_ADJUST_REL_TOL = 0.005          # median fetched/stored price ratio drifting this far
                                 # from 1.0 ⇒ the series was re-adjusted, refetch all


_QUOTE_FIELDS = (
    "company,insref,name,diff1d,diff1dprc,diff3mprc,diffytdprc,diff3yprc,diff5yprc,"
    "bidprice,askprice,lastprice,openprice,closeprice1d,dayhighprice,daylowprice,quantity,numdec,"
    "description,tradecurrency,per,psr,pbr,sps,eps,dps,bvps,dividendyield,"
    "om,pm,gm,companyname,marketcap,sectorl3name,ceo,chairman,"
    "numberofshares,totalnumberofshares,address,email,website,isin"
)


def fetch_quote(insref):
    """
    Returns a rich quote dict for one stock: price, fundamentals, margins,
    performance returns, and company info. Returns {"error": ...} on failure.
    """
    url = (
        f"{_BASE}?cmd=quote&fields={_QUOTE_FIELDS}"
        f"&lang=sv&filetype=json&token={TOKEN}&insref={insref}"
    )
    try:
        raw  = urlopen(url, timeout=8).read().decode("utf-8")
        data = json.loads(raw)
    except Exception as e:
        return {"error": str(e)}
    if not data:
        return {"error": "no data"}
    return data[0]


def fetch_recommendations(insref):
    """
    Returns analyst consensus and price target for the most recent quarter.
    {period, buy, hold, sell, target_avg, target_min, target_max, target_count, currency}
    Returns {"error": ...} on failure or missing data.
    """
    url = (
        f"{_BASE}?cmd=recommendations"
        f"&fields=aspect,average,min,max,count,field,insref,name,symbol,company,currency"
        f"&filetype=json&token={TOKEN}&insref={insref}"
    )
    try:
        raw  = urlopen(url, timeout=8).read().decode("utf-8")
        data = json.loads(raw)
    except Exception as e:
        return {"error": str(e)}

    if not data:
        return {"error": "no data"}

    recs = data[0].get("recommendations", [])

    # Most recent quarter that has Rec entries
    periods = sorted({r["period"] for r in recs if r["field"] == "Rec"}, reverse=True)
    if not periods:
        return {"error": "no periods"}

    latest = periods[0]
    buy = hold = sell = 0
    target_avg = target_min = target_max = target_count = currency = None

    for r in recs:
        if r["period"] != latest:
            continue
        if r["field"] == "Rec":
            aspect = str(r.get("aspect", "")).strip()
            count  = r.get("count") or 0
            if aspect in ("1", "2"):   buy  += count
            elif aspect == "3":        hold += count
            elif aspect in ("4", "5"): sell += count
        elif r["field"] == "Target":
            target_avg   = r.get("average")
            target_min   = r.get("min")
            target_max   = r.get("max")
            target_count = r.get("count")
            currency     = r.get("currency")

    return {
        "period":       latest,
        "buy":          buy,
        "hold":         hold,
        "sell":         sell,
        "target_avg":   target_avg,
        "target_min":   target_min,
        "target_max":   target_max,
        "target_count": target_count,
        "currency":     currency,
    }


def fetch_orderbook(insref):
    """
    Returns the current order book for a stock.
    {tradestate, numdec, bid: [{level, price, quantity}], ask: [{level, price, quantity}]}
    Returns {"error": ...} on failure.
    """
    url = (
        f"{_BASE}?cmd=orderbook"
        f"&fields=price,quantity,tradestate,numdec"
        f"&filetype=json&token={TOKEN}&insref={insref}"
    )
    try:
        raw  = urlopen(url, timeout=8).read().decode("utf-8")
        data = json.loads(raw)
    except Exception as e:
        return {"error": str(e)}
    if not data:
        return {"error": "no data"}
    d = data[0]
    return {
        "tradestate": d.get("tradestate"),
        "numdec":     d.get("numdec", 2),
        "bid":        d.get("bid", []),
        "ask":        d.get("ask", []),
    }


_LIST_ID_MAP = {
    "35207": "large cap",
    "35208": "mid cap",
    "35209": "small cap",
}

def fetch_list(insref):
    """
    Fetch canonical market list for a stock.
    Returns one of: 'large cap', 'mid cap', 'small cap', 'first north', 'spotlight', or ''.
    Uses list IDs for Stockholm cap tiers; marketplacename for other venues.
    """
    url = (
        f"{_BASE}?cmd=quote&fields=marketplacename,list"
        f"&lang=sv&filetype=json&token={TOKEN}&insref={insref}"
    )
    try:
        raw  = urlopen(url, timeout=8).read().decode("utf-8")
        data = json.loads(raw)
        if not data or not isinstance(data, list):
            return ""
        d = data[0]
        list_ids    = set(d.get("list", "").split())
        marketplace = d.get("marketplacename", "").lower()
        for lid, canonical in _LIST_ID_MAP.items():
            if lid in list_ids:
                return canonical
        if "first north" in marketplace:
            return "first north"
        if "spotlight" in marketplace:
            return "spotlight"
    except Exception:
        pass
    return ""


def fetch_history(insref, startdate=_HISTORY_EPOCH):
    """
    Fetches daily OHLC history via the historychart endpoint.
    Returns a list of [ts_ms, closeprice, closequantity] rows (oldest first),
    or {"error": ...} on failure.

    startdate bounds the request. The default pulls everything, which for a
    long-listed name is ~750 KB; passing a recent date instead costs a few KB.
    Millistream honours the bound loosely and tends to return a couple of extra
    weeks before it, so a bounded request still overlaps stored data — see
    sync_history(), which relies on that overlap.
    """
    url = (
        f"{_BASE}?widget=historychart&token={TOKEN}&target=buildwidget_0"
        f"&fields=name,tradecurrency,date,closeprice,closequantity"
        f"&language=sv&insref={insref}&startdate={startdate}"
        f"&intradaylen=7&xhr=0&adjusted=1"
    )
    try:
        raw = urlopen(url, timeout=15).read().decode("utf-8")
    except Exception as e:
        return {"error": str(e)}

    try:
        start = raw.index("([") + 1
        end   = raw.rindex("])")
        data  = json.loads(raw[start:end + 1])
    except (ValueError, json.JSONDecodeError) as e:
        return {"error": f"parse error: {e}"}

    if not data:
        return {"error": "no data"}

    rows = []
    for entry in data[0].get("history", []):
        try:
            dt    = datetime.strptime(entry["date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            ts_ms = int(dt.timestamp() * 1000)
            rows.append([ts_ms, float(entry["closeprice"]),
                         float(entry.get("closequantity") or 0)])
        except (KeyError, ValueError):
            continue
    return rows


def history_path(name, insref, output_dir="."):
    return os.path.join(output_dir, f"{name}_{insref}_History.csv")


def _read_history_tail(path, max_bytes=_TAIL_BYTES):
    """
    Parse the last rows of an append-only History CSV without reading all of it.
    The file is oldest-first, so its tail is always the most recent data.

    Returns a list of (byte_offset, ts_ms, closeprice), oldest first, where
    byte_offset is where that row's line starts — letting a caller truncate the
    file at a chosen bar and rewrite from there. Returns [] if the file is
    missing, empty or unreadable.
    """
    try:
        size = os.path.getsize(path)
        if not size:
            return []
        with open(path, "rb") as f:
            base = 0
            if size > max_bytes:
                base = size - max_bytes
                f.seek(base)
                base += len(f.readline())   # discard the partial line we landed in
            blob = f.read()
    except OSError:
        return []

    out, off = [], base
    for line in blob.splitlines(keepends=True):
        row = next(csv.reader([line.decode("utf-8", "replace")]), None)
        if row and len(row) >= 2:
            try:
                out.append((off, int(float(row[0])), float(row[1])))
            except ValueError:
                pass
        off += len(line)
    return out


def sync_history(name, insref, output_dir=".", force_full=False):
    """
    Bring {name}_{insref}_History.csv up to date using the smallest request that
    will do. Given existing local data, only the recent window is requested — a
    few KB, versus ~750 KB to pull a long-listed name from 1970.

    The fetched window overlaps what we already stored, and that overlap is
    rewritten rather than skipped. Plain append-and-dedupe cannot do this: a bar
    first captured mid-session holds a provisional close, and once its timestamp
    is on disk a deduping append will never correct it.

    The overlap also detects re-adjustment. A split or dividend rescales every
    historical close, so stored prices go stale all the way back and the whole
    file has to be refetched. Individual bars differing is normal (that is the
    provisional-close staleness above), so the test is the *median* ratio of
    fetched to stored price across the window — that sits at exactly 1.0 for
    ordinary staleness and shifts wholesale when a series is re-adjusted.

    Returns {"added": int, "mode": "incremental"|"full", "reason": str}, where
    "added" counts rows written (rewritten window bars included).
    """
    path = history_path(name, insref, output_dir)
    tail = [] if force_full else _read_history_tail(path)

    def _full(reason, rewrite=True):
        rows = fetch_history(insref)
        if not isinstance(rows, list) or not rows:
            return {"added": 0, "mode": "full", "reason": f"{reason}: refetch failed"}
        added = update_history_csv(name, insref, rows, output_dir=output_dir,
                                   rewrite=rewrite)
        return {"added": added, "mode": "full", "reason": reason}

    if not tail:
        return _full("forced" if force_full else "no local history",
                     rewrite=force_full)

    last_date = datetime.fromtimestamp(tail[-1][1] / 1000, tz=timezone.utc).date()
    startdate = (last_date - timedelta(days=_OVERLAP_DAYS)).isoformat()
    rows = fetch_history(insref, startdate=startdate)
    if not isinstance(rows, list) or not rows:
        return {"added": 0, "mode": "incremental", "reason": "fetch failed"}

    stored  = {ts: close for _, ts, close in tail}
    ratios  = sorted(close / stored[int(ts)] for ts, close, _ in rows
                     if stored.get(int(ts)))
    if not ratios:
        # Window came back disjoint from our data. We cannot check for
        # re-adjustment, and appending could splice two price scales together.
        return _full("no overlap with stored history")

    if abs(ratios[len(ratios) // 2] - 1.0) > _ADJUST_REL_TOL:
        return _full("series re-adjusted")

    window_start = int(rows[0][0])
    if window_start < tail[0][1]:
        # Window reaches back past the tail we read, so we cannot locate its
        # start to truncate at. Rare; rebuild rather than risk duplicate rows.
        return _full("window predates tail")

    if all(stored.get(int(ts)) == close for ts, close, _ in rows):
        return {"added": 0, "mode": "incremental", "reason": "already current"}

    # Truncate at the first stored bar the window covers, then rewrite the window.
    cut = next(off for off, ts, _ in tail if ts >= window_start)
    try:
        with open(path, "r+b") as f:
            f.truncate(cut)
    except OSError:
        return {"added": 0, "mode": "incremental", "reason": "truncate failed"}
    with open(path, "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(rows)
    print(f"{name}_{insref}_History.csv  window rewritten, {len(rows)} rows")
    return {"added": len(rows), "mode": "incremental", "reason": "window rewritten"}


def update_history_csv(name, insref, rows, output_dir=".", rewrite=False):
    """
    Write daily history rows to {name}_{insref}_History.csv and return how many
    rows were written.

    Append-only by default, deduplicating by timestamp so re-running never
    creates duplicate rows. With rewrite=True the file is replaced wholesale,
    which is what a re-adjusted series needs — appending to it would leave the
    old, differently-adjusted prices in place.
    """
    path = history_path(name, insref, output_dir)

    if rewrite:
        with open(path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(rows)
        print(f"{name}_{insref}_History.csv  rewritten, {len(rows)} rows")
        return len(rows)

    seen = set()
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.reader(f):
                if row:
                    try:
                        seen.add(int(float(row[0])))
                    except ValueError:
                        pass
    new_rows = [r for r in rows if int(r[0]) not in seen]
    if new_rows:
        with open(path, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(new_rows)
    print(f"{name}_{insref}_History.csv  +{len(new_rows)} new rows")
    return len(new_rows)
