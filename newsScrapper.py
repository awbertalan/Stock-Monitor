from urllib.request import urlopen
from html.parser import HTMLParser
from datetime import datetime, timedelta, timezone
import json, re, os
from dotenv import load_dotenv
load_dotenv()

TOKEN = os.environ["MILLISTREAM_TOKEN"]
_BASE = "https://mws-2.millistream.com/mws.fcgi"

_NEWSTYPE_LABELS = {
    0: "Analyst",
    1: "Regulatory",
    2: "Press Release",
    3: "Calendar",
    4: "Other",
}


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
    m = re.search(r"<body>(.*?)</body>", xml_text, re.DOTALL)
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


def fetch_news(insref, limit=20, days_back=90):
    """
    Returns a list of news items for the given insref (newest first):
    [{newsid, newstype, newstype_label, date, time, headline, source, body}]
    Returns {"error": ...} on failure or empty list if no news found.
    """
    startdate = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%d")
    url = (
        f"{_BASE}?cmd=news"
        f"&fields=newsid,newstype,date,time,headline,company,symbol,text"
        f"&limit={limit}&timezone=Europe%2FStockholm"
        f"&startdate={startdate}&insref={insref}"
        f"&filetype=json&token={TOKEN}&lang=sv"
    )
    try:
        raw  = urlopen(url, timeout=10).read().decode("utf-8")
        data = json.loads(raw)
    except Exception as e:
        return {"error": str(e)}

    if not isinstance(data, list):
        return {"error": "unexpected response"}

    result = []
    for item in data:
        result.append({
            "newsid":        item.get("newsid"),
            "newstype":      item.get("newstype"),
            "newstype_label": _NEWSTYPE_LABELS.get(item.get("newstype"), "News"),
            "date":          item.get("date", ""),
            "time":          (item.get("time") or "")[:5],
            "headline":      item.get("headline", ""),
            "source":        item.get("symbol", ""),
            "body":          _parse_body(item.get("text", "")),
        })
    return result
