from urllib.request import urlopen
import csv, os, re, time, json, threading
import env_loader
env_loader.load_env()

_TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'stock_template.html')
_NAMES_CSV     = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'stock_names.csv')
_names_lock    = threading.Lock()
TOKEN          = os.environ["MILLISTREAM_TOKEN"]
_BASE          = "https://mws-2.millistream.com/mws.fcgi"


def _fetch_isin(insref):
    """Fetch ISIN for a given insref from Millistream quote endpoint. Returns "" if not found."""
    try:
        insref = int(insref)
        url = f"{_BASE}?cmd=quote&fields=isin&filetype=json&token={TOKEN}&insref={insref}&lang=sv"
        raw = urlopen(url, timeout=8).read().decode("utf-8")
        data = json.loads(raw)
        if data and isinstance(data, list) and len(data) > 0:
            isin = data[0].get("isin")
            if isin and isinstance(isin, str) and len(isin.strip()) > 0:
                return isin.strip()
    except (ValueError, TypeError):
        pass
    except json.JSONDecodeError:
        pass
    except Exception:
        pass
    return ""


def upsert_name_csv(insref, raw_name, isin=None, mlist=None):
    """Update or insert insref -> raw_name, isin, list in stock_names.csv (4 columns)."""
    try:
        insref = int(insref)
        raw_name = str(raw_name).strip()
        if not raw_name:
            return
    except (ValueError, TypeError):
        return

    with _names_lock:
        names = {}
        try:
            with open(_NAMES_CSV, newline='', encoding='utf-8') as f:
                for row in csv.reader(f):
                    if len(row) >= 2:
                        try:
                            iref = int(row[0])
                            nm = row[1].strip()
                            isin_val  = row[2].strip() if len(row) >= 3 else ""
                            mlist_val = row[3].strip() if len(row) >= 4 else ""
                            if nm:
                                names[iref] = (nm, isin_val, mlist_val)
                        except (ValueError, IndexError):
                            pass
        except FileNotFoundError:
            pass
        except Exception:
            pass

        existing = names.get(insref, ("", "", ""))
        isin  = str(isin).strip()  if isin  else ""
        mlist = str(mlist).strip() if mlist else ""
        isin  = isin  or existing[1]
        mlist = mlist or existing[2]

        names[insref] = (raw_name, isin, mlist)

        try:
            with open(_NAMES_CSV, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                for iref, (nm, is_val, lst) in sorted(names.items()):
                    writer.writerow([iref, nm, is_val or "", lst or ""])
        except Exception:
            pass


def _esc(s):
    return str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')


def _generate_stock_html(output_dir, name, insref, rel_path, inst_type='', currency='', display_name=None):
    with open(_TEMPLATE_PATH, encoding='utf-8') as f:
        tpl = f.read()

    shown_name = display_name if display_name else name
    stock_json = json.dumps({
        'name': shown_name, 'insref': insref,
        'type': inst_type, 'currency': currency,
        'csv7d': f'{rel_path}/{name}_{insref}_7d.csv',
    })
    type_badge = f'<span class="badge badge-type">{_esc(inst_type)}</span>' if inst_type else ''
    curr_badge = f'<span class="badge badge-curr">{_esc(currency)}</span>' if currency else ''

    html = (tpl
        .replace('{{NAME}}',       _esc(shown_name))
        .replace('{{INSREF}}',     str(insref))
        .replace('{{TYPE_BADGE}}', type_badge)
        .replace('{{CURR_BADGE}}', curr_badge)
        .replace('{{STOCK_JSON}}', stock_json))

    with open(os.path.join(output_dir, 'index.html'), 'w', encoding='utf-8') as f:
        f.write(html)

def fetch_stock(insref):
    url = (
        f"https://mws-2.millistream.com/mws.fcgi?widget=intradaychart"
        f"&token={TOKEN}&target=buildwidget_0"
        f"&fields=name,tradecurrency,time,date,tradeprice,tradequantity,"
        f"marketopen,marketclose,closeprice1d&language=sv&compress=1"
        f"&insref={insref}&intradaylen=7&xhr=0&adjusted=1"
    )
    html = urlopen(url, timeout=10).read().decode("utf-8")

    end     = ":["
    end_idx = html.find(end)
    if end_idx < 0:
        raise ValueError(f"fetch_stock({insref}): unexpected response format, ':[' not found")

    try:
        header     = html[17:end_idx].split(',')
        insref_val = int(header[0].split(':')[1])
        raw_name   = header[1].split(':')[1].strip('"')
    except (IndexError, ValueError) as e:
        raise ValueError(f"fetch_stock({insref}): could not parse header: {e}") from e
    name = re.sub(r'[^a-zA-Z0-9]', '', raw_name)

    start = end_idx + 3
    data_str = html[start:].removesuffix('}]}]);')
    entries = data_str.removeprefix('{').split('},{')

    rows = []
    for entry in entries:
        parts = re.split(r'[:,]+', entry)
        try:
            rows.append([float(parts[1]), float(parts[3]), float(parts[5])])
        except (IndexError, ValueError):
            continue

    return name, insref_val, rows, raw_name


def update_csvs(name, insref, rows, output_dir='.', rel_path=None, inst_type='', currency='', display_name=None):
    base = os.path.join(output_dir, f"{name}_{insref}")

    # Full history — append rows whose timestamp isn't already in the file
    full_path = f"{base}.csv"
    seen = set()
    if os.path.exists(full_path):
        with open(full_path, newline='', encoding='utf-8') as f:
            for row in csv.reader(f):
                if row:
                    try:
                        seen.add(int(float(row[0])))
                    except ValueError:
                        pass

    new_rows = [r for r in rows if int(r[0]) not in seen]
    if new_rows:
        with open(full_path, 'a', newline='', encoding='utf-8') as f:
            csv.writer(f).writerows(new_rows)

    # 7-day window — overwrite with only the last 7 days of rows
    cutoff_ms = (time.time() - 7 * 24 * 3600) * 1000
    seven_day_rows = [r for r in rows if r[0] >= cutoff_ms]
    with open(f"{base}_7d.csv", 'w', newline='', encoding='utf-8') as f:
        csv.writer(f).writerows(seven_day_rows)

    if rel_path:
        _generate_stock_html(output_dir, name, insref, rel_path, inst_type, currency, display_name)

    print(
        f"{name}_{insref}.csv  +{len(new_rows)} new rows  |  "
        f"{name}_{insref}_7d.csv  {len(seven_day_rows)} rows"
    )


if __name__ == '__main__':
    name, insref, rows, raw_name = fetch_stock(1034561)
    upsert_name_csv(insref, raw_name)
    update_csvs(name, insref, rows)
