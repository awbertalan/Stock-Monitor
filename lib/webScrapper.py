from urllib.request import urlopen
from contextlib import contextmanager
import csv, os, re, time, json, threading
try:
    import fcntl                      # POSIX only; absent on Windows
except ImportError:                   # pragma: no cover - not a target platform
    fcntl = None
import env_loader
import engine_common as ec
env_loader.load_env()

_ROOT          = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # project root (this module lives in lib/)
_TEMPLATE_PATH = os.path.join(_ROOT, 'web', 'stock_template.html')
_NAMES_CSV     = os.path.join(_ROOT, 'stock_names.csv')
_names_lock    = threading.Lock()
# Separate lockfile rather than flock on stock_names.csv itself: every write
# ends in os.replace, which swaps the inode out from under any lock held on the
# old one.
_NAMES_LOCKFILE = _NAMES_CSV + '.lock'
TOKEN          = os.environ["MILLISTREAM_TOKEN"]
_BASE          = "https://mws-2.millistream.com/mws.fcgi"


@contextmanager
def _names_file_lock(timeout=30.0):
    """Serialise the stock_names.csv read-modify-write ACROSS processes.

    _names_lock only covers threads inside one interpreter, and this file has
    writers in more than one: app.py's refresh sweep (upsert_names_bulk at the
    end of a sweep that can run for many minutes) and scripts/tag_sectors.py
    (upsert_sectors_bulk). Because _write_names_csv is atomic, a writer holding
    a stale snapshot never produces a torn file — it silently reinstates it, so
    sector tags (column 5, feeding sector_concentration) or cap tiers (column 3,
    which build_universe filters all three engines on) written meanwhile just
    vanish with nothing logged.

    Best effort by design: acquired non-blocking with retries, and if the lock
    cannot be created or is still held after `timeout` we log and proceed
    unserialised rather than stalling a refresh worker forever. That is no worse
    than the behaviour this replaces.
    """
    if fcntl is None:
        yield False
        return
    try:
        fd = os.open(_NAMES_LOCKFILE, os.O_CREAT | os.O_RDWR, 0o644)
    except OSError as e:
        print(f" stock_names.csv: lockfile unavailable ({e!r}) — writing unserialised")
        yield False
        return
    held     = False
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    print(f" stock_names.csv: still locked by another process after "
                          f"{timeout:.0f}s — writing anyway (last writer wins)")
                    break
                time.sleep(0.1)
        yield held
    finally:
        try:
            if held:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


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


def _read_names_csv():
    """Return {insref: (name, isin, mlist, sector_id, sector_name)} from
    stock_names.csv, or {} if absent.

    Columns 4 (sector_id) and 5 (sector_name) were appended 2026-08-31. They are
    read and re-written here purely so a refresh sweep cannot silently drop them:
    every writer funnels through _write_names_csv, which would otherwise emit the
    original four columns and wipe the sector tags on the next Update All."""
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
                        sec_id    = row[4].strip() if len(row) >= 5 else ""
                        sec_name  = row[5].strip() if len(row) >= 6 else ""
                        if nm:
                            names[iref] = (nm, isin_val, mlist_val, sec_id, sec_name)
                    except (ValueError, IndexError):
                        pass
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return names


def _write_names_csv(names):
    """Replace stock_names.csv atomically.

    Written to a temp file and renamed rather than opened 'w' in place: a full
    rewrite takes ~80 ms, and readers (app._load_names, the live engines) would
    otherwise be able to open the file mid-write and see a truncated universe.
    """
    tmp = f"{_NAMES_CSV}.tmp"
    try:
        with open(tmp, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            for iref, rec in sorted(names.items()):
                nm, is_val, lst = rec[0], rec[1], rec[2]
                sec_id   = rec[3] if len(rec) > 3 else ""
                sec_name = rec[4] if len(rec) > 4 else ""
                writer.writerow([iref, nm, is_val or "", lst or "",
                                 sec_id or "", sec_name or ""])
        os.replace(tmp, _NAMES_CSV)
    except Exception as e:
        # Never swallow this silently. A failed rewrite used to leave callers
        # reporting success ("1,980 written") against an unchanged file, which
        # is indistinguishable from the write having worked until something
        # much later reads the file back and finds the columns missing.
        print(f" stock_names.csv write FAILED: {e!r}")
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _merge_name(names, insref, raw_name, isin=None, mlist=None):
    """Merge one entry into a names map, keeping any isin/list already known.
    Returns True if the entry was usable and applied."""
    try:
        insref = int(insref)
        raw_name = str(raw_name).strip()
        if not raw_name:
            return False
    except (ValueError, TypeError):
        return False

    existing = names.get(insref, ("", "", "", "", ""))
    isin  = str(isin).strip()  if isin  else ""
    mlist = str(mlist).strip() if mlist else ""
    # Sector is never supplied by the scrape/refresh callers — it is written only
    # by scripts/tag_sectors.py. Carry whatever is already on the row so an
    # ordinary name/isin/list update does not clear it.
    sec_id   = existing[3] if len(existing) > 3 else ""
    sec_name = existing[4] if len(existing) > 4 else ""
    names[insref] = (raw_name, isin or existing[1], mlist or existing[2],
                     sec_id, sec_name)
    return True


def upsert_name_csv(insref, raw_name, isin=None, mlist=None):
    """Update or insert insref -> raw_name, isin, list in stock_names.csv.
    Sector columns (4,5), when present, are preserved by _merge_name."""
    with _names_lock, _names_file_lock():
        names = _read_names_csv()
        if _merge_name(names, insref, raw_name, isin, mlist):
            _write_names_csv(names)


def upsert_names_bulk(updates):
    """Apply many insref -> name updates in a single read + write of the file.

    upsert_name_csv rewrites all ~59k rows on every call and holds _names_lock
    throughout, so calling it once per stock both dominated a full refresh sweep
    (~47% of its wall clock) and serialised any concurrent workers. Batching
    turns tens of thousands of rewrites into one.

    updates: iterable of (insref, raw_name) or (insref, raw_name, isin, mlist).
    Returns the number of entries applied.
    """
    with _names_lock, _names_file_lock():
        names   = _read_names_csv()
        applied = 0
        for upd in updates:
            upd = tuple(upd)
            if len(upd) < 2:
                continue
            if _merge_name(names, upd[0], upd[1],
                           upd[2] if len(upd) > 2 else None,
                           upd[3] if len(upd) > 3 else None):
                applied += 1
        if applied:
            _write_names_csv(names)
        return applied


def upsert_sectors_bulk(updates):
    """Write sector tags onto existing rows in a single read + write.

    updates: iterable of (insref, sector_id, sector_name).

    Only touches columns 4 and 5 — name/isin/list are left exactly as they are,
    and an insref not already in the file is ignored rather than inserted (the
    sector feed knows about instruments the universe deliberately excludes).
    Returns the number of rows updated.
    """
    with _names_lock, _names_file_lock():
        names   = _read_names_csv()
        applied = 0
        for upd in updates:
            upd = tuple(upd)
            if len(upd) < 3:
                continue
            try:
                iref = int(upd[0])
            except (ValueError, TypeError):
                continue
            rec = names.get(iref)
            if rec is None:
                continue
            sec_id   = str(upd[1]).strip() if upd[1] not in (None, "") else ""
            sec_name = str(upd[2]).strip() if upd[2] not in (None, "") else ""
            if not sec_name:
                continue
            if len(rec) > 4 and rec[3] == sec_id and rec[4] == sec_name:
                continue                      # already tagged, nothing to write
            names[iref] = (rec[0], rec[1], rec[2], sec_id, sec_name)
            applied += 1
        if applied:
            _write_names_csv(names)
        return applied


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
    seven_path = f"{base}_7d.csv"

    # Refuse to replace a populated 7d file with nothing.
    #
    # fetch_stock only raises when the ':[' marker or the header is missing. A
    # payload whose field order shifted still "parses": every entry falls into
    # the per-entry `except (IndexError, ValueError): continue` and the call
    # returns rows=[] like a normal quiet stock. Opening this file in 'w' then
    # truncated it to zero bytes before anyone knew the fetch was any good —
    # and one Update All does that to the whole universe in a single sweep.
    # These minute prints exist nowhere else: the deep archive keeps daily
    # closes, and the feed only serves the trailing 7 days, so the loss is
    # permanent. It is also invisible: this file is the sole price input of the
    # live_patterns engine (load_intraday_minute_closes globs *_7d.csv), which
    # would find no bars, detect no patterns, log a clean run and quietly stop
    # trading.
    #
    # The trigger is `not rows` — nothing parsed at all, which is the evidence
    # of the bad fetch above — and NOT an empty 7-day window. A thin name whose
    # newest print sits just outside the cutoff still parses fine and returns
    # rows; that is "the stock was quiet", not "the feed broke", and it falls
    # through to the normal empty write below so the file self-clears exactly
    # as it always did. Gating on the empty window instead froze week-old
    # prints on disk and re-reported an error on every 15-minute sweep for any
    # instrument that simply stopped trading.
    #
    # Refuse by returning False rather than raising: update_csvs is the first
    # call in each caller's per-stock try block and the callers do more work
    # after it — infoScrapper.sync_history (the daily History.csv live_signals
    # reads), the name/index upserts. Unwinding them would stop the DAILY
    # record too, and under the universe-wide payload shift this guard exists
    # to defend against that would starve live_signals as well.
    if not rows and os.path.exists(seven_path) and os.path.getsize(seven_path) > 0:
        print(
            f" {name}_{insref}_7d.csv: fetch parsed 0 rows at all — "
            f"keeping the {os.path.getsize(seven_path)} bytes already on disk"
        )
        return False

    # Atomic swap rather than open('w'): the destination is only ever the old
    # file or the complete new one, never a half-written one.
    ec.write_rows_atomic(seven_path, seven_day_rows)

    if rel_path:
        _generate_stock_html(output_dir, name, insref, rel_path, inst_type, currency, display_name)

    print(
        f"{name}_{insref}.csv  +{len(new_rows)} new rows  |  "
        f"{name}_{insref}_7d.csv  {len(seven_day_rows)} rows"
    )
    return True


if __name__ == '__main__':
    name, insref, rows, raw_name = fetch_stock(1034561)
    upsert_name_csv(insref, raw_name)
    update_csvs(name, insref, rows)
