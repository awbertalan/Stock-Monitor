from http.server import BaseHTTPRequestHandler, HTTPServer
import threading, webbrowser, json, urllib.parse, os, csv, time, re
import webController
import webScrapper
import infoScrapper
import newsScrapper
import tradesScrapper

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))

OMXS30_NAMES = {
    'ABB', 'AlfaLaval', 'AssaAbloyB', 'AstraZeneca',
    'AtlasCopcoA', 'AtlasCopcoB', 'Boliden', 'ElectroluxB',
    'EpirocA', 'EpirocB', 'EricssonB', 'EssityB', 'Evolution',
    'GetingeB', 'HMB', 'HexagonB', 'IndustrivrdenC',
    'InvestorB', 'KinnevikB', 'NibeIndustrierB', 'NordeaBank',
    'Sandvik', 'SEBA', 'Sinch', 'SkanskaB', 'SKFB',
    'HandelsbankenA', 'Tele2B', 'TeliaCompany', 'VolvoB',
}
INST_DIR   = os.path.join(BASE_DIR, "Instrumenttype")

MARKET_INDICES = [
    {"name": "OMXS30",    "path": "Index/SEK/6485_OMXStockholm30Index",        "fname": "OMXStockholm30Index_6485_7d.csv"},
    {"name": "Dow Jones", "path": "Index/USD/39485_DowJonesIndustrialAverage",  "fname": "DowJonesIndustrialAverage_39485_7d.csv"},
    {"name": "S&P 500",   "path": "Index/USD/72823_SP500",                      "fname": "SP500_72823_7d.csv"},
    {"name": "NASDAQ 100","path": "Index/USD/39486_NASDAQ100",                  "fname": "NASDAQ100_39486_7d.csv"},
    {"name": "DAX",       "path": "Index/EUR/72822_DAX",                        "fname": "DAX_72822_7d.csv"},
]
INDEX_PATH     = os.path.join(BASE_DIR, "stock_index.json")
HISTORY_PATH   = os.path.join(BASE_DIR, "scrape_history.json")
SETTINGS_PATH  = os.path.join(BASE_DIR, "settings.json")
WATCHLIST_PATH = os.path.join(BASE_DIR, "watchlist.json")
ALERTS_PATH    = os.path.join(BASE_DIR, "alerts.json")
NAMES_CSV_PATH = os.path.join(BASE_DIR, "stock_names.csv")


def _load_names():
    """Return {insref_int: (raw_name, isin, mlist)} from stock_names.csv (4 columns)."""
    names = {}
    try:
        with open(NAMES_CSV_PATH, newline='', encoding='utf-8') as f:
            for row in csv.reader(f):
                if len(row) >= 2:
                    try:
                        insref = int(row[0])
                        name   = row[1]
                        isin   = row[2] if len(row) >= 3 else ""
                        mlist  = row[3] if len(row) >= 4 else ""
                        names[insref] = (name, isin, mlist)
                    except ValueError:
                        pass
    except FileNotFoundError:
        pass
    return names

_watchlist_lock     = threading.Lock()
_alerts_lock        = threading.Lock()
_dashboard_cache    = {}
_dashboard_cache_lock = threading.Lock()


def _read_watchlist():
    try:
        with open(WATCHLIST_PATH, encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _write_watchlist(wl):
    with open(WATCHLIST_PATH, 'w', encoding='utf-8') as f:
        json.dump(wl, f, ensure_ascii=False)


def _read_alerts():
    try:
        with open(ALERTS_PATH, encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _write_alerts(alerts):
    with open(ALERTS_PATH, 'w', encoding='utf-8') as f:
        json.dump(alerts, f, ensure_ascii=False, indent=2)

_DEFAULT_SETTINGS = {
    "refresh_interval_s": 30,
    "trades_limit": 25,
    "update_filter": {"types": None, "currencies": None},
    "auto_refresh_weekend_minutes": 240,
    "auto_refresh_market_minutes": 15,
    "auto_refresh_off_hours_minutes": 60,
    "market_hours_start": 9,
    "market_hours_end": 17,
}


def _load_settings():
    try:
        with open(SETTINGS_PATH, encoding='utf-8') as f:
            return {**_DEFAULT_SETTINGS, **json.load(f)}
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(_DEFAULT_SETTINGS)

_index_lock          = threading.Lock()
_refresh_all_status  = {"running": False, "done": 0, "total": 0}
_backfill_status     = {"running": False, "done": 0, "total": 0, "fetched": 0, "skipped": 0, "errors": 0, "message": ""}
_isin_status         = {"running": False, "done": 0, "total": 0, "updated": 0, "failed": 0, "elapsed_s": 0, "message": ""}
_list_status         = {"running": False, "done": 0, "total": 0, "updated": 0, "failed": 0, "skipped": 0, "elapsed_s": 0, "message": ""}


def _scan_currencies():
    currencies = set()
    if not os.path.isdir(INST_DIR):
        return []
    for inst_type in os.listdir(INST_DIR):
        type_dir = os.path.join(INST_DIR, inst_type)
        if not os.path.isdir(type_dir):
            continue
        for currency in os.listdir(type_dir):
            if os.path.isdir(os.path.join(type_dir, currency)):
                currencies.add(currency)
    return sorted(currencies)


def _scan_all_stocks():
    stocks = []
    if not os.path.isdir(INST_DIR):
        return stocks
    for inst_type in sorted(os.listdir(INST_DIR)):
        type_dir = os.path.join(INST_DIR, inst_type)
        if not os.path.isdir(type_dir):
            continue
        for currency in sorted(os.listdir(type_dir)):
            currency_dir = os.path.join(type_dir, currency)
            if not os.path.isdir(currency_dir):
                continue
            for folder in sorted(os.listdir(currency_dir)):
                stock_dir = os.path.join(currency_dir, folder)
                if not os.path.isdir(stock_dir):
                    continue
                if not any(f.endswith('.csv') for f in os.listdir(stock_dir)):
                    continue
                sep = folder.find('_')
                if sep < 0:
                    continue
                try:
                    insref = int(folder[:sep])
                except ValueError:
                    continue
                stocks.append({
                    "insref":   insref,
                    "name":     folder[sep + 1:],
                    "type":     inst_type,
                    "currency": currency,
                    "path":     f"{inst_type}/{currency}/{folder}",
                })
    return stocks


def _upsert_index(entry):
    with _index_lock:
        try:
            with open(INDEX_PATH, encoding='utf-8') as f:
                index = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            index = []
        for i, e in enumerate(index):
            if e.get("insref") == entry["insref"]:
                index[i] = entry
                break
        else:
            index.append(entry)
        with open(INDEX_PATH, 'w', encoding='utf-8') as f:
            json.dump(index, f, ensure_ascii=False)


def _do_refresh_all(update_filter=None):
    # Always scan the directory so every stock with CSV data is included,
    # regardless of what the index file currently knows about.
    stocks = _scan_all_stocks()
    with _index_lock:
        with open(INDEX_PATH, 'w', encoding='utf-8') as f:
            json.dump(stocks, f, ensure_ascii=False)
    if isinstance(update_filter, dict):
        type_filter = update_filter.get("types")
        curr_filter = update_filter.get("currencies")
        if type_filter is not None:
            stocks = [s for s in stocks if s["type"] in type_filter]
        if curr_filter is not None:
            stocks = [s for s in stocks if s["currency"] in curr_filter]
    _refresh_all_status["total"] = len(stocks)
    _refresh_all_status["done"]  = 0
    for stock in stocks:
        try:
            stock_dir = os.path.normpath(os.path.join(INST_DIR, stock["path"]))
            if stock_dir.startswith(INST_DIR):
                _, _, rows, raw_name = webScrapper.fetch_stock(stock["insref"])
                webScrapper.upsert_name_csv(stock["insref"], raw_name)
                webScrapper.update_csvs(
                    stock["name"], stock["insref"], rows,
                    output_dir=stock_dir, rel_path=stock["path"],
                    inst_type=stock["type"], currency=stock["currency"],
                    display_name=raw_name,
                )
                hist = infoScrapper.fetch_history(stock["insref"])
                if isinstance(hist, list):
                    infoScrapper.update_history_csv(
                        stock["name"], stock["insref"], hist, output_dir=stock_dir
                    )
        except Exception:
            pass
        _refresh_all_status["done"] += 1
    _refresh_all_status["running"] = False
    with _dashboard_cache_lock:
        _dashboard_cache.clear()

def _do_backfill():
    stocks = _scan_all_stocks()
    _backfill_status.update({
        "running": True, "done": 0, "total": len(stocks),
        "fetched": 0, "skipped": 0, "errors": 0, "message": "Running…",
    })
    for stock in stocks:
        insref    = stock["insref"]
        name      = stock["name"]
        stock_dir = os.path.normpath(os.path.join(INST_DIR, stock["path"]))
        hist_path = os.path.join(stock_dir, f"{name}_{insref}_History.csv")
        if os.path.exists(hist_path):
            _backfill_status["skipped"] += 1
        else:
            try:
                rows = infoScrapper.fetch_history(insref)
                if isinstance(rows, list) and rows:
                    infoScrapper.update_history_csv(name, insref, rows, output_dir=stock_dir)
                    _backfill_status["fetched"] += 1
                else:
                    _backfill_status["errors"] += 1
            except Exception:
                _backfill_status["errors"] += 1
        _backfill_status["done"] += 1
    _backfill_status["running"] = False
    _backfill_status["message"] = (
        f"Done — {_backfill_status['fetched']} fetched, "
        f"{_backfill_status['skipped']} already existed, "
        f"{_backfill_status['errors']} errors."
    )


_CANONICAL_LISTS = {"large cap", "mid cap", "small cap", "first north", "spotlight"}

def _do_update_lists():
    names = _load_names()
    _list_status.update({
        "running": True, "done": 0, "total": len(names),
        "updated": 0, "failed": 0, "skipped": 0, "elapsed_s": 0, "message": "Running…",
    })
    t0 = time.time()
    for insref, (name, isin, mlist) in names.items():
        try:
            if mlist.lower() in _CANONICAL_LISTS:
                _list_status["skipped"] += 1
            else:
                new_list = infoScrapper.fetch_list(insref)
                if new_list:
                    webScrapper.upsert_name_csv(insref, name, isin, new_list)
                    _list_status["updated"] += 1
                else:
                    _list_status["failed"] += 1
                time.sleep(0.05)
        except Exception:
            _list_status["failed"] += 1
        _list_status["done"] += 1
    elapsed = round(time.time() - t0)
    _list_status["running"]   = False
    _list_status["elapsed_s"] = elapsed
    _list_status["message"]   = (
        f"Done — {_list_status['updated']} updated, "
        f"{_list_status['skipped']} already had list, "
        f"{_list_status['failed']} not found. ({elapsed}s)"
    )


def _do_update_isins():
    names = _load_names()
    _isin_status.update({
        "running": True, "done": 0, "total": len(names),
        "updated": 0, "failed": 0, "elapsed_s": 0, "message": "Running…",
    })
    t0 = time.time()
    for insref, (name, isin, _) in names.items():
        try:
            if not isin:
                new_isin = webScrapper._fetch_isin(insref)
                if new_isin:
                    webScrapper.upsert_name_csv(insref, name, new_isin)
                    _isin_status["updated"] += 1
                else:
                    _isin_status["failed"] += 1
                time.sleep(0.1)
        except Exception:
            _isin_status["failed"] += 1
        _isin_status["done"] += 1
    elapsed = round(time.time() - t0)
    _isin_status["running"]   = False
    _isin_status["elapsed_s"] = elapsed
    _isin_status["message"]   = (
        f"Done — {_isin_status['updated']} updated, "
        f"{_isin_status['failed']} not found. ({elapsed}s)"
    )


def _read_market_context():
    """Return list of {name, price, dod_pct, last_ts_ms} for all MARKET_INDICES."""
    import datetime as _dt
    _UTC = _dt.timezone.utc
    results = []
    for idx in MARKET_INDICES:
        csv_path = os.path.join(INST_DIR, idx["path"], idx["fname"])
        rows = []
        try:
            with open(csv_path, newline='', encoding='utf-8') as f:
                for row in csv.reader(f):
                    try:
                        rows.append((float(row[0]), float(row[1])))
                    except (ValueError, IndexError):
                        pass
        except OSError:
            pass
        if not rows:
            results.append({"name": idx["name"], "price": None, "dod_pct": None, "last_ts_ms": None})
            continue
        last_ts, last_price = rows[-1]
        last_date  = _dt.datetime.fromtimestamp(last_ts / 1000, tz=_UTC).date()
        prev_rows  = [(ts, p) for ts, p in rows if _dt.datetime.fromtimestamp(ts / 1000, tz=_UTC).date() < last_date]
        prev_close = prev_rows[-1][1] if prev_rows else rows[0][1]
        dod_pct    = (last_price - prev_close) / prev_close * 100 if prev_close else 0
        results.append({
            "name":       idx["name"],
            "price":      last_price,
            "dod_pct":    round(dod_pct, 2),
            "last_ts_ms": last_ts,
        })
    return results


def _do_refresh_indices():
    for idx in MARKET_INDICES:
        parts = idx["path"].strip("/").split("/")
        if len(parts) != 3:
            continue
        inst_type, currency, folder = parts
        stock_dir = os.path.normpath(os.path.join(INST_DIR, idx["path"]))
        if not stock_dir.startswith(INST_DIR):
            continue
        try:
            sep    = folder.index("_")
            insref = int(folder[:sep])
            name   = folder[sep + 1:]
            threading.Thread(
                target=_do_refresh_one,
                args=(idx["path"], inst_type, currency, stock_dir, insref, name),
                daemon=True,
            ).start()
        except (ValueError, AttributeError):
            pass


def _do_refresh_one(rel_path, inst_type, currency, stock_dir, insref, name):
    try:
        _, _, rows, raw_name = webScrapper.fetch_stock(insref)
        webScrapper.update_csvs(name, insref, rows, output_dir=stock_dir,
                                rel_path=rel_path, inst_type=inst_type, currency=currency,
                                display_name=raw_name)
        webScrapper.upsert_name_csv(insref, raw_name)
        _upsert_index({"insref": insref, "name": name, "type": inst_type,
                       "currency": currency, "path": rel_path})
    except Exception:
        pass


STATIC = {
    "/style.css":          ("text/css; charset=utf-8",          "style.css"),
    "/stock_page.js":      ("text/javascript; charset=utf-8",   "stock_page.js"),
    "/pattern_engine.js":  ("text/javascript; charset=utf-8",   "patternEngine.js"),
    "/nav.js":             ("text/javascript; charset=utf-8",   "nav.js"),
    "/":               ("text/html; charset=utf-8",          "dashboard.html"),
    "/scrape":         ("text/html; charset=utf-8",          "index.html"),
    "/browse":         ("text/html; charset=utf-8",          "browse.html"),
    "/stocksearch":    ("text/html; charset=utf-8",          "stocksearch.html"),
    "/settings":       ("text/html; charset=utf-8",          "settings.html"),
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path == "/status":
            self._json(webController.status)
        elif parsed.path == "/scrape-history":
            try:
                with open(HISTORY_PATH, encoding='utf-8') as f:
                    self._json(json.load(f))
            except (FileNotFoundError, json.JSONDecodeError):
                self._json([])
        elif parsed.path == "/refresh-all-status":
            self._json(_refresh_all_status)
        elif parsed.path == "/backfill-history-status":
            self._json(_backfill_status)
        elif parsed.path == "/update-isins-status":
            self._json(_isin_status)
        elif parsed.path == "/update-lists-status":
            self._json(_list_status)
        elif parsed.path == "/market-context":
            self._json(_read_market_context())
        elif parsed.path == "/dashboard-data":
            params = urllib.parse.parse_qs(parsed.query)
            self._dashboard_data(params.get("mode", [""])[0])
        elif parsed.path == "/search":
            params = urllib.parse.parse_qs(parsed.query)
            self._search(params.get("q", [""])[0])
        elif parsed.path == "/view":
            params = urllib.parse.parse_qs(parsed.query)
            self._stock_page(params.get("path", [""])[0])
        elif parsed.path == "/csv":
            params = urllib.parse.parse_qs(parsed.query)
            self._csv(params.get("path", [""])[0])
        elif parsed.path == "/ls":
            params = urllib.parse.parse_qs(parsed.query)
            self._ls(params.get("path", [""])[0])
        elif parsed.path == "/get-settings":
            self._json(_load_settings())
        elif parsed.path == "/instrument-types":
            self._json(webController.insttype_list)
        elif parsed.path == "/currencies":
            self._json(_scan_currencies())
        elif parsed.path == "/watchlist":
            with _watchlist_lock:
                self._json(_read_watchlist())
        elif parsed.path == "/alerts":
            with _alerts_lock:
                self._json(_read_alerts())
        elif parsed.path == "/recommendations":
            params = urllib.parse.parse_qs(parsed.query)
            self._recommendations(params.get("insref", [""])[0])
        elif parsed.path == "/quote":
            params = urllib.parse.parse_qs(parsed.query)
            insref = params.get("insref", [""])[0]
            if insref:
                self._json(infoScrapper.fetch_quote(insref))
            else:
                self._json({"error": "missing insref"})
        elif parsed.path == "/orderbook":
            params = urllib.parse.parse_qs(parsed.query)
            insref = params.get("insref", [""])[0]
            if insref:
                self._json(infoScrapper.fetch_orderbook(insref))
            else:
                self._json({"error": "missing insref"})
        elif parsed.path == "/news":
            params = urllib.parse.parse_qs(parsed.query)
            insref = params.get("insref", [""])[0]
            if insref:
                self._json(newsScrapper.fetch_news(insref))
            else:
                self._json({"error": "missing insref"})
        elif parsed.path == "/trades":
            params = urllib.parse.parse_qs(parsed.query)
            insref = params.get("insref", [""])[0]
            limit = params.get("limit", ["1000"])[0]
            if insref:
                try:
                    limit = int(limit)
                except ValueError:
                    limit = 1000
                self._json(tradesScrapper.fetch_trades(insref, limit=limit))
            else:
                self._json({"error": "missing insref"})
        elif parsed.path in STATIC:
            mime, filename = STATIC[parsed.path]
            self._file(mime, os.path.join(BASE_DIR, filename))
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw    = self.rfile.read(length)

        if self.path == "/settings":
            try:
                self._save_settings(json.loads(raw.decode()))
            except Exception as e:
                self._json({"error": str(e)})
            return
        if self.path == "/watchlist":
            try:
                data   = json.loads(raw.decode())
                path   = str(data.get("path", ""))
                action = data.get("action", "add")
                with _watchlist_lock:
                    wl = _read_watchlist()
                    if action == "add" and path not in wl:
                        wl.append(path)
                    elif action == "remove" and path in wl:
                        wl.remove(path)
                    _write_watchlist(wl)
                    with _dashboard_cache_lock:
                        _dashboard_cache.pop('favorites', None)
                self._json({"ok": True, "watchlist": wl})
            except Exception as e:
                self._json({"error": str(e)})
            return
        if self.path == "/alerts":
            try:
                data   = json.loads(raw.decode())
                action = data.get("action", "add")
                path   = str(data.get("path", ""))
                with _alerts_lock:
                    alerts = _read_alerts()
                    if action == "remove":
                        alerts = [a for a in alerts if a.get("path") != path]
                    elif action == "add":
                        alerts = [a for a in alerts if a.get("path") != path]
                        alerts.append({
                            "path":      path,
                            "name":      str(data.get("name", "")),
                            "condition": str(data.get("condition", "above")),
                            "target":    float(data.get("target", 0)),
                        })
                    _write_alerts(alerts)
                self._json({"ok": True, "alerts": alerts})
            except Exception as e:
                self._json({"error": str(e)})
            return
        if self.path == "/update-isins":
            if not _isin_status["running"]:
                threading.Thread(target=_do_update_isins, daemon=True).start()
                self._json({"started": True})
            else:
                self._json({"error": "already running"})
            return
        if self.path == "/update-lists":
            if not _list_status["running"]:
                threading.Thread(target=_do_update_lists, daemon=True).start()
                self._json({"started": True})
            else:
                self._json({"error": "already running"})
            return

        body = urllib.parse.parse_qs(raw.decode())

        if self.path == "/stop":
            webController.stop_search()
            self.send_response(200)
            self.end_headers()
        elif self.path == "/start" and not webController.status["running"]:
            start_index = int(body.get("start", [0])[0])
            end_index   = int(body.get("end",   [1000])[0])
            threading.Thread(target=webController.run_search, args=(start_index, end_index), daemon=True).start()
            self.send_response(200)
            self.end_headers()
        elif self.path == "/refresh":
            self._refresh(body.get("path", [""])[0])
        elif self.path == "/refresh-indices":
            _do_refresh_indices()
            self._json({"ok": True, "count": len(MARKET_INDICES)})
        elif self.path == "/refresh-all":
            if not _refresh_all_status["running"]:
                uf = _load_settings().get("update_filter")
                update_filter = uf if isinstance(uf, dict) else None
                _refresh_all_status["running"] = True
                _refresh_all_status["done"]    = 0
                _refresh_all_status["total"]   = 0
                threading.Thread(target=_do_refresh_all, args=(update_filter,), daemon=True).start()
                self._json({"started": True})
            else:
                self._json({"error": "already running"})
        elif self.path == "/refresh-all-full":
            if not _refresh_all_status["running"]:
                _refresh_all_status["running"] = True
                _refresh_all_status["done"]    = 0
                _refresh_all_status["total"]   = 0
                threading.Thread(target=_do_refresh_all, args=(None,), daemon=True).start()
                self._json({"started": True})
            else:
                self._json({"error": "already running"})
        elif self.path == "/backfill-history":
            if not _backfill_status["running"]:
                threading.Thread(target=_do_backfill, daemon=True).start()
                self._json({"started": True})
            else:
                self._json({"error": "already running"})
        else:
            self.send_response(200)
            self.end_headers()

    def _refresh(self, rel_path):
        parts = rel_path.strip("/").split("/")
        if len(parts) != 3:
            self._json({"error": "Invalid path"})
            return
        inst_type, currency, folder = parts
        stock_dir = os.path.normpath(os.path.join(INST_DIR, rel_path))
        if not stock_dir.startswith(INST_DIR):
            self._json({"error": "Access denied"})
            return
        try:
            sep    = folder.index("_")
            insref = int(folder[:sep])
            name   = folder[sep + 1:]
        except (ValueError, AttributeError):
            self._json({"error": "Cannot parse stock folder name"})
            return
        threading.Thread(
            target=_do_refresh_one,
            args=(rel_path, inst_type, currency, stock_dir, insref, name),
            daemon=True,
        ).start()
        self._json({"ok": True})

    def _save_settings(self, new_settings):
        validated = {}
        if "refresh_interval_s" in new_settings:
            validated["refresh_interval_s"] = max(0, int(new_settings["refresh_interval_s"]))
        if "trades_limit" in new_settings:
            validated["trades_limit"] = max(10, min(50, int(new_settings["trades_limit"])))
        if "update_filter" in new_settings:
            f = new_settings["update_filter"]
            if isinstance(f, dict):
                validated["update_filter"] = {
                    "types":      list(f["types"])      if isinstance(f.get("types"),      list) else None,
                    "currencies": list(f["currencies"]) if isinstance(f.get("currencies"), list) else None,
                }
            else:
                validated["update_filter"] = {"types": None, "currencies": None}
        if "auto_refresh_weekend_minutes" in new_settings:
            validated["auto_refresh_weekend_minutes"] = max(1, int(new_settings["auto_refresh_weekend_minutes"]))
        if "auto_refresh_market_minutes" in new_settings:
            validated["auto_refresh_market_minutes"] = max(1, int(new_settings["auto_refresh_market_minutes"]))
        if "auto_refresh_off_hours_minutes" in new_settings:
            validated["auto_refresh_off_hours_minutes"] = max(1, int(new_settings["auto_refresh_off_hours_minutes"]))
        if "market_hours_start" in new_settings:
            validated["market_hours_start"] = max(0, min(23, int(new_settings["market_hours_start"])))
        if "market_hours_end" in new_settings:
            validated["market_hours_end"] = max(0, min(23, int(new_settings["market_hours_end"])))
        existing = _load_settings()
        existing.update(validated)
        with open(SETTINGS_PATH, 'w', encoding='utf-8') as fp:
            json.dump(existing, fp, ensure_ascii=False, indent=2)
        self._json({"ok": True})

    def _file(self, mime, path):
        try:
            with open(path, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.end_headers()
            self.wfile.write(data)
        except FileNotFoundError:
            self.send_response(404)
            self.end_headers()

    def _json(self, data):
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def _stock_page(self, rel_path):
        if not rel_path:
            self.send_response(302)
            self.send_header("Location", "/stocksearch")
            self.end_headers()
            return
        target = os.path.normpath(os.path.join(INST_DIR, rel_path, "index.html"))
        if not target.startswith(INST_DIR):
            self.send_response(403)
            self.end_headers()
            return
        if not os.path.isfile(target):
            self.send_response(404)
            self.end_headers()
            return

        # Extract the STOCK JSON baked into the existing index.html and
        # re-render from the current stock_template.html so that template
        # changes (new panels, scripts, etc.) are always live.
        try:
            with open(target, encoding='utf-8') as f:
                old_html = f.read()
            m = re.search(r'const STOCK = ({.*?});', old_html)
            stock = json.loads(m.group(1)) if m else {}
        except Exception:
            stock = {}

        try:
            tpl_path = os.path.join(BASE_DIR, 'stock_template.html')
            with open(tpl_path, encoding='utf-8') as f:
                tpl = f.read()

            import html as _html
            shown_name  = stock.get('name', '')
            insref      = stock.get('insref', '')
            inst_type   = stock.get('type', '')
            currency    = stock.get('currency', '')

            # Inject csvHist if the _History.csv has been generated
            csv7d = stock.get('csv7d', '')
            if csv7d:
                csv_hist_rel = csv7d.replace('_7d.csv', '_History.csv')
                if os.path.isfile(os.path.join(INST_DIR, csv_hist_rel)):
                    stock['csvHist'] = csv_hist_rel

            stock_json  = json.dumps(stock)
            type_badge  = f'<span class="badge badge-type">{_html.escape(inst_type)}</span>' if inst_type else ''
            curr_badge  = f'<span class="badge badge-curr">{_html.escape(currency)}</span>' if currency else ''

            rendered = (tpl
                .replace('{{NAME}}',       _html.escape(shown_name))
                .replace('{{INSREF}}',     str(insref))
                .replace('{{TYPE_BADGE}}', type_badge)
                .replace('{{CURR_BADGE}}', curr_badge)
                .replace('{{STOCK_JSON}}', stock_json))

            body = rendered.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            # Fallback to the static file if template rendering fails
            self._file("text/html; charset=utf-8", target)

    def _csv(self, rel_path):
        if not rel_path.endswith(".csv"):
            self._json({"error": "Invalid file type"})
            return
        target = os.path.normpath(os.path.join(INST_DIR, rel_path))
        if not target.startswith(INST_DIR):
            self._json({"error": "Access denied"})
            return
        if not os.path.isfile(target):
            self._json({"error": "File not found"})
            return
        rows = []
        with open(target, newline="", encoding="utf-8") as f:
            for row in csv.reader(f):
                if len(row) >= 2:
                    try:
                        rows.append([float(row[0]), float(row[1]), float(row[2]) if len(row) > 2 else 0.0])
                    except ValueError:
                        pass
        self._json(rows)

    def _dashboard_data(self, mode=''):
        cache_key = mode or ''
        with _dashboard_cache_lock:
            if cache_key in _dashboard_cache:
                self._json(_dashboard_cache[cache_key])
                return

        results = []
        if not os.path.isdir(INST_DIR):
            self._json(results)
            return

        names           = _load_names()
        watchlist_paths = set(_read_watchlist())

        _LIST_MODES = {
            'spotlight':   'spotlight',
            'first_north': 'first north',
            'small_cap':   'small cap',
            'mid_cap':     'mid cap',
            'large_cap':   'large cap',
        }

        for inst_type in sorted(os.listdir(INST_DIR)):
            type_dir = os.path.join(INST_DIR, inst_type)
            if not os.path.isdir(type_dir):
                continue
            for currency in sorted(os.listdir(type_dir)):
                currency_dir = os.path.join(type_dir, currency)
                if not os.path.isdir(currency_dir):
                    continue
                for stock_folder in sorted(os.listdir(currency_dir)):
                    stock_dir = os.path.join(currency_dir, stock_folder)
                    if not os.path.isdir(stock_dir):
                        continue

                    sep_idx    = stock_folder.find('_')
                    cname      = stock_folder[sep_idx + 1:] if sep_idx >= 0 else stock_folder
                    stock_path = f'{inst_type}/{currency}/{stock_folder}'
                    try:
                        insref_int = int(stock_folder[:sep_idx]) if sep_idx >= 0 else -1
                    except ValueError:
                        insref_int = -1

                    if mode == 'omxs30':
                        if not (inst_type == 'Equity' and currency == 'SEK' and cname in OMXS30_NAMES):
                            continue
                    elif mode == 'favorites':
                        if stock_path not in watchlist_paths:
                            continue
                    elif mode in _LIST_MODES:
                        mlist = names.get(insref_int, ("", "", ""))[2].lower() if insref_int >= 0 else ""
                        if _LIST_MODES[mode] not in mlist:
                            continue

                    seven_d = next(
                        (f for f in sorted(os.listdir(stock_dir)) if f.endswith('_7d.csv')),
                        None
                    )
                    if not seven_d:
                        continue
                    prices = []
                    try:
                        with open(os.path.join(stock_dir, seven_d), newline='', encoding='utf-8') as f:
                            for row in csv.reader(f):
                                if len(row) >= 2:
                                    try:
                                        prices.append(float(row[1]))
                                    except ValueError:
                                        pass
                    except OSError:
                        continue
                    if not prices:
                        continue
                    first_p = prices[0]
                    last_p  = prices[-1]
                    change  = last_p - first_p
                    pct     = (change / first_p * 100) if first_p else 0
                    step    = max(1, len(prices) // 30)
                    spark   = prices[::step]
                    if spark[-1] != prices[-1]:
                        spark.append(prices[-1])

                    display_name = names.get(insref_int, (cname,))[0] if insref_int >= 0 else cname
                    results.append({
                        'name':     display_name,
                        'insref':   stock_folder[:sep_idx] if sep_idx >= 0 else '',
                        'type':     inst_type,
                        'currency': currency,
                        'path':     stock_path,
                        'price':    last_p,
                        'change':   change,
                        'pct':      pct,
                        'spark':    spark,
                    })
        with _dashboard_cache_lock:
            _dashboard_cache[cache_key] = results
        self._json(results)

    def _search(self, query):
        results = []
        q = query.strip().lower()
        if not os.path.isdir(INST_DIR):
            self._json(results)
            return
        names = _load_names()
        for inst_type in sorted(os.listdir(INST_DIR)):
            type_dir = os.path.join(INST_DIR, inst_type)
            if not os.path.isdir(type_dir):
                continue
            for currency in sorted(os.listdir(type_dir)):
                currency_dir = os.path.join(type_dir, currency)
                if not os.path.isdir(currency_dir):
                    continue
                for stock_folder in sorted(os.listdir(currency_dir)):
                    stock_dir = os.path.join(currency_dir, stock_folder)
                    if not os.path.isdir(stock_dir):
                        continue
                    csvs = sorted(f for f in os.listdir(stock_dir) if f.endswith(".csv"))
                    if not csvs:
                        continue
                    sep    = stock_folder.find('_')
                    try:
                        insref_int   = int(stock_folder[:sep]) if sep >= 0 else -1
                        display_name = names.get(insref_int, (stock_folder[sep + 1:] if sep >= 0 else stock_folder,))[0]
                    except ValueError:
                        display_name = stock_folder[sep + 1:] if sep >= 0 else stock_folder
                    if q and (
                        q not in stock_folder.lower() and
                        q not in display_name.lower() and
                        q not in inst_type.lower() and
                        q not in currency.lower()
                    ):
                        continue
                    results.append({
                        "folder":   stock_folder,
                        "name":     display_name,
                        "type":     inst_type,
                        "currency": currency,
                        "csvs":     csvs,
                        "path":     f"{inst_type}/{currency}/{stock_folder}",
                    })
        self._json(results)

    def _recommendations(self, insref):
        if not insref:
            self._json({"error": "missing insref"})
            return
        self._json(infoScrapper.fetch_recommendations(insref))

    def _ls(self, rel_path):
        target = os.path.normpath(os.path.join(INST_DIR, rel_path)) if rel_path else INST_DIR
        if not target.startswith(INST_DIR):
            self._json({"error": "Access denied"})
            return
        if not os.path.isdir(target):
            self._json({"entries": [], "path": "", "parent": None})
            return
        entries = [
            {"name": name, "is_dir": os.path.isdir(os.path.join(target, name))}
            for name in sorted(os.listdir(target))
        ]
        rel = os.path.relpath(target, INST_DIR)
        current = "" if rel == "." else rel
        parent = None
        if current:
            parent_rel = os.path.relpath(os.path.dirname(target), INST_DIR)
            parent = "" if parent_rel == "." else parent_rel
        self._json({"path": current, "entries": entries, "parent": parent})


def _auto_refresh_loop():
    import datetime as _dt
    while True:
        settings = _load_settings()
        now = _dt.datetime.now()
        if now.weekday() >= 5:          # Saturday=5, Sunday=6
            interval = settings.get("auto_refresh_weekend_minutes", 240) * 60
            label    = f"{settings.get('auto_refresh_weekend_minutes', 240)}-min (weekend)"
        elif settings.get("market_hours_start", 9) <= now.hour < settings.get("market_hours_end", 17):
            interval = settings.get("auto_refresh_market_minutes", 15) * 60
            label    = f"{settings.get('auto_refresh_market_minutes', 15)}-min (market hours)"
        else:
            interval = settings.get("auto_refresh_off_hours_minutes", 60) * 60
            label    = f"{settings.get('auto_refresh_off_hours_minutes', 60)}-min (off-hours)"
        time.sleep(interval)
        if not _refresh_all_status["running"]:
            uf = settings.get("update_filter")
            update_filter = uf if isinstance(uf, dict) else None
            _refresh_all_status["running"] = True
            _refresh_all_status["done"]    = 0
            _refresh_all_status["total"]   = 0
            print(f"[auto-refresh] Starting {label} scheduled update…")
            _do_refresh_all(update_filter)
            print("[auto-refresh] Scheduled update complete.")


def _startup_refresh():
    print("[startup] Starting full update of all stocks…")
    _refresh_all_status["running"] = True
    _refresh_all_status["done"]    = 0
    _refresh_all_status["total"]   = 0
    _do_refresh_all(None)
    print("[startup] Full startup update complete.")


PORT = 8765
threading.Thread(target=_auto_refresh_loop, daemon=True).start()
threading.Thread(target=_startup_refresh, daemon=True).start()
server = HTTPServer(("0.0.0.0", PORT), Handler)
print(f"Opening browser at http://localhost:{PORT}")
webbrowser.open(f"http://localhost:{PORT}")
server.serve_forever()
