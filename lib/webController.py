from urllib.request import urlopen
import os, re, time, datetime, threading, json, csv
import webScrapper
import infoScrapper
# webScrapper / infoScrapper both call env_loader.load_env() at module load,
# so MILLISTREAM_TOKEN is already in os.environ by the time we read it here.
TOKEN = os.environ["MILLISTREAM_TOKEN"]

insttype_list = ["Marketplace","List","Company","News Agency","Equity",
                  "Derivative","Index","Exchange Traded Fund","Mutual Fund",
                  "Rights","Forex","Fixed Income","Money Market","Real Estate",
                  "Structured Product","Warrant","Uncategorized Type",
                  "Exchange Traded Commodity","Unit Trust Certificate",
                  "Primary Capital Certificate","Classification Sector",
                  "Commodity","Exchange Traded Certificate",
                  "Tick Table","Submarket","Implied Volatility Instruments"]

stocklist      = []
stocklist_lock = threading.Lock()
_stop_requested = False

status = {
    "running":       False,
    "message":       "Ready.",
    "phase":         "",
    "checked":       0,
    "total":         0,
    "found":         0,
    "start_index":   0,
    "end_index":     0,
    "stopped_early": False,
    "scrape_done":   0,
    "scrape_total":  0,
}


def stop_search():
    global _stop_requested
    _stop_requested = True
    status["message"] = "Stop requested — finishing current requests…"


def _write_history(entry, base_dir):
    path = os.path.join(base_dir, "scrape_history.json")
    try:
        with open(path, encoding='utf-8') as f:
            history = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        history = []
    history.append(entry)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def run_search(start_index, end_index):
    global _stop_requested
    base_dir = os.path.dirname(os.path.abspath(__file__))
    _stop_requested = False
    stocklist.clear()

    status.update({
        "running":       True,
        "message":       f"Searching {start_index} to {end_index}…",
        "phase":         "searching",
        "checked":       0,
        "total":         end_index - start_index + 1,
        "found":         0,
        "start_index":   start_index,
        "end_index":     end_index,
        "stopped_early": False,
        "scrape_done":   0,
        "scrape_total":  0,
    })
    starttime = time.time()

    # Phase 1 — parallel index search
    num_threads = 8
    chunk = max(1, (end_index - start_index + 1) // num_threads)
    ranges = [
        (start_index + i * chunk,
         start_index + (i + 1) * chunk - 1 if i < num_threads - 1 else end_index)
        for i in range(num_threads)
    ]
    threads = [threading.Thread(target=urlcheck, args=(s, e)) for s, e in ranges]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    stopped_early = _stop_requested
    status["stopped_early"] = stopped_early

    # Phase 2 — create folder structure (always runs, even after early stop)
    status["phase"]   = "building"
    status["message"] = (
        f"Building folders for {len(stocklist)} stocks"
        + (" (early stop)" if stopped_early else "") + "…"
    )
    folder(base_dir)
    new_folders, skipped_folders = 0, 0
    for stock in stocklist:
        n, s = dircheck(stock, base_dir)
        new_folders += n
        skipped_folders += s

    # Phase 3 — scrape price data (always runs)
    status["phase"]        = "scraping"
    status["scrape_total"] = len(stocklist)
    status["scrape_done"]  = 0
    status["message"]      = f"Scraping price data for {len(stocklist)} stocks…"
    scraped, scrape_errors = 0, 0
    for stock in stocklist:
        insref, name, tradecurrency, instrumenttype, raw_name = stock
        stock_dir = os.path.join(
            base_dir, "Instrumenttype",
            insttype_list[instrumenttype], tradecurrency,
            f"{insref}_{name}"
        )
        try:
            _, _, rows, _ = webScrapper.fetch_stock(insref)
            inst_type_name = insttype_list[instrumenttype]
            rel_path = f"{inst_type_name}/{tradecurrency}/{insref}_{name}"
            webScrapper.update_csvs(name, insref, rows, output_dir=stock_dir,
                                    rel_path=rel_path, inst_type=inst_type_name,
                                    currency=tradecurrency, display_name=raw_name)
            hist = infoScrapper.fetch_history(insref)
            if isinstance(hist, list):
                infoScrapper.update_history_csv(name, insref, hist, output_dir=stock_dir)
            scraped += 1
        except Exception:
            scrape_errors += 1
        status["scrape_done"] = scraped + scrape_errors

    # Write stock index
    index = [
        {
            "insref":   s[0],
            "name":     s[1],
            "type":     insttype_list[s[3]],
            "currency": s[2],
            "path":     f"{insttype_list[s[3]]}/{s[2]}/{s[0]}_{s[1]}",
        }
        for s in stocklist
    ]
    with open(os.path.join(base_dir, "stock_index.json"), 'w', encoding='utf-8') as f:
        json.dump(index, f, ensure_ascii=False)

    # Update master names CSV (merge with any existing entries)
    names_path = os.path.join(base_dir, "stock_names.csv")
    names_map = {}
    try:
        with open(names_path, newline='', encoding='utf-8') as f:
            for row in csv.reader(f):
                if len(row) >= 2:
                    try:
                        names_map[int(row[0])] = row[1]
                    except ValueError:
                        pass
    except FileNotFoundError:
        pass
    for s in stocklist:
        names_map[s[0]] = s[4]
    with open(names_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        for iref, nm in sorted(names_map.items()):
            writer.writerow([iref, nm])

    # Write history record
    duration_s = int(time.time() - starttime)
    _write_history({
        "date":          datetime.datetime.now().strftime("%Y-%m-%d"),
        "time":          datetime.datetime.now().strftime("%H:%M:%S"),
        "start_index":   start_index,
        "end_index":     end_index,
        "checked":       status["checked"],
        "found":         len(stocklist),
        "stopped_early": stopped_early,
        "duration_s":    duration_s,
        "scraped":       scraped,
        "scrape_errors": scrape_errors,
    }, base_dir)

    totaltime  = datetime.timedelta(seconds=duration_s)
    early_note = " (early stop)" if stopped_early else ""
    status["message"] = (
        f"Done{early_note} in {totaltime} | "
        f"Checked: {status['checked']}/{end_index - start_index + 1} | "
        f"Found: {len(stocklist)}\n"
        f"Folders created: {new_folders} | Skipped: {skipped_folders}\n"
        f"Scraped: {scraped} | Scrape errors: {scrape_errors}"
    )
    status["phase"]   = "done"
    status["running"] = False


def folder(base_dir):
    inst_dir = os.path.join(base_dir, "Instrumenttype")
    os.makedirs(inst_dir, exist_ok=True)
    for name in insttype_list:
        os.makedirs(os.path.join(inst_dir, name), exist_ok=True)


def dircheck(stock, base_dir):
    insref, name, tradecurrency, instrumenttype, *_ = stock
    new, skipped = 0, 0
    currency_dir = os.path.join(base_dir, "Instrumenttype", insttype_list[instrumenttype], tradecurrency)
    if not os.path.exists(currency_dir):
        os.mkdir(currency_dir)
        new += 1
    else:
        skipped += 1
    stock_dir = os.path.join(currency_dir, f"{insref}_{name}")
    if not os.path.exists(stock_dir):
        os.mkdir(stock_dir)
        new += 1
    else:
        skipped += 1
    return new, skipped


def urlinfo(page):
    html_bytes = page.read()
    html = html_bytes.decode("utf-8")
    end  = ":["
    info = html[17: html.find(end)].split(',')
    insref         = info[0].split(':')
    name           = info[1].split(':')
    raw_name       = name[1].strip('"')
    clnname        = re.sub(r'[^a-zA-Z0-9]', '', raw_name)
    tradecurrency  = info[2].split(':')
    instrumenttype = info[5].split(':')
    stock = [int(insref[1]), clnname, 
             tradecurrency[1].strip("\""), int(instrumenttype[1])]
    stocklist.append(stock)


def urlcheck(start_index, end_index):
    global _stop_requested
    i = start_index
    while i <= end_index and not _stop_requested:
        url = (
            f"https://mws-2.millistream.com/mws.fcgi?widget=intradaychart"
            f"&token={TOKEN}&target=buildwidget_0"
            f"&fields=name,tradecurrency,time,date,tradeprice,tradequantity,"
            f"marketopen,marketclose,closeprice1d&language=sv&compress=1"
            f"&insref={i}&intradaylen=7&xhr=0&adjusted=1"
        )
        try:
            page = urlopen(url, timeout=8)
            urlinfo(page)
            status["found"] = len(stocklist)
        except Exception:
            pass
        status["checked"] += 1
        i += 1
