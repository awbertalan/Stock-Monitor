from urllib.request import urlopen
from datetime import datetime, timezone
import json, csv, os
import env_loader
env_loader.load_env()

TOKEN = os.environ["MILLISTREAM_TOKEN"]
_BASE = "https://mws-2.millistream.com/mws.fcgi"


_QUOTE_FIELDS = (
    "company,insref,name,diff1d,diff1dprc,diff3mprc,diffytdprc,diff3yprc,diff5yprc,"
    "bidprice,askprice,lastprice,closeprice1d,dayhighprice,daylowprice,quantity,numdec,"
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


def fetch_history(insref):
    """
    Fetches full daily OHLC history via the historychart endpoint.
    Returns a list of [ts_ms, closeprice, closequantity] rows (oldest first),
    or {"error": ...} on failure.
    """
    url = (
        f"{_BASE}?widget=historychart&token={TOKEN}&target=buildwidget_0"
        f"&fields=name,tradecurrency,date,closeprice,closequantity"
        f"&language=sv&insref={insref}&startdate=1970-01-01"
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


def update_history_csv(name, insref, rows, output_dir="."):
    """
    Append-only write of daily history rows to {name}_{insref}_History.csv.
    Deduplicates by timestamp so re-running never creates duplicate rows.
    """
    path = os.path.join(output_dir, f"{name}_{insref}_History.csv")
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
