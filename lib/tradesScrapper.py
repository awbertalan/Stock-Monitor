from urllib.request import urlopen
import json, os
import env_loader
env_loader.load_env()

TOKEN = os.environ["MILLISTREAM_TOKEN"]
_BASE = "https://mws-2.millistream.com/mws.fcgi"


def fetch_trades(insref, limit=1000):
    """
    Fetch recent trade data for a given stock.
    Returns {insref, numdec, trade: [list of trades]} or {"error": ...} on failure.
    
    Each trade includes: date, time, tradeprice, tradequantity, tradereference
    """
    url = (
        f"{_BASE}?cmd=trades"
        f"&fields=date,time,tradeprice,tradequantity,tradereference,numdec"
        f"&filetype=json&token={TOKEN}"
        f"&timezone=Europe%2FStockholm"
        f"&insref={insref}"
        f"&orderby=date,time&order=desc,desc"
        f"&limit={limit}"
    )
    try:
        raw = urlopen(url, timeout=10).read().decode("utf-8")
        data = json.loads(raw)
    except Exception as e:
        return {"error": str(e)}

    if not data or not isinstance(data, list):
        return {"error": "no data"}

    return data[0]
