"""
Live paper-trading signal generator.

Strategy: +0.5% in <= 5 trading days, with a strict pullback entry.
  Entry (computed on latest close, refreshed each run with the live quote):
    1) 2 consecutive down days        (close[D-1] < close[D-2] AND close[D] < close[D-1])
    2) close[D] in bottom 20% of last-5-day range
    3) RSI(14) < 35
    4) 20-day return > -10%           (skip crash regimes)
  Sizing:    target 100k SEK / position, floor 50k, cap 120k, wallet 500k
  Exits:     TP +0.5%, SL -2.0%, time-out at 5 trading days

Universe:
  Primary   = every OMX large cap with sufficient history (auto-loaded each
              run from stock_names.csv, so re-classifications drop out
              automatically).
  Watchlist = every OMX mid/small cap with sufficient history. We do NOT take
              positions here (order size > available volume), but we surface
              any signal so the user can manually evaluate.

State is persisted in live_portfolio.json so this script can be invoked
repeatedly (e.g. every 15 min) without double-entering positions.
"""

import csv
import glob
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta

import env_loader
env_loader.load_env()
import infoScrapper  # noqa: E402

ROOT        = os.path.dirname(os.path.abspath(__file__))
BASE        = os.path.join(ROOT, "Instrumenttype", "Equity", "SEK")
STATE_PATH  = os.path.join(ROOT, "live_portfolio.json")
NAMES_CSV   = os.path.join(ROOT, "stock_names.csv")

WALLET_START  = 500_000.0
POS_TARGET    = 100_000.0
POS_MIN       =  50_000.0
POS_MAX       = 120_000.0

TP_PCT        = 0.005   # +0.5%
SL_PCT        = -0.020  # -2.0%
MAX_HOLD_DAYS = 5
RSI_PERIOD    = 14
RSI_MAX       = 35
DEPTH_OF_PB   = 0.20    # close must be in bottom 20% of 5-day range
CRASH_GUARD   = -0.10   # require 20-day ret > -10%

LIVE_FETCH_WORKERS = 10
MIN_HISTORY_DAYS   = 30


# ----------------------------------------------------------------------------- helpers

def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            return json.load(f)
    return {
        "cash":              WALLET_START,
        "positions":         {},
        "closed_trades":     [],
        "last_run":          None,
        "last_universe":     [],
        "last_watchlist":    [],
    }


def save_state(state):
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2, default=str)


def load_history_closes(path):
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.reader(f):
            if not r:
                continue
            try:
                ts_ms = int(float(r[0]))
                price = float(r[1])
                rows.append((ts_ms, price))
            except (ValueError, IndexError):
                pass
    rows.sort(key=lambda x: x[0])
    return rows


def rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    window = closes[-(period + 1):]
    gains = losses = 0.0
    for i in range(1, len(window)):
        ch = window[i] - window[i - 1]
        if ch > 0:
            gains += ch
        else:
            losses -= ch
    avg_g = gains / period
    avg_l = losses / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100 - 100 / (1 + rs)


def add_trading_days(start_date, n):
    d = start_date
    added = 0
    while added < n:
        d += timedelta(days=1)
        if d.weekday() < 5:
            added += 1
    return d


def signal_check(closes):
    if len(closes) < 21:
        return False, {"err": "insufficient history"}
    today, yest, yest2 = closes[-1], closes[-2], closes[-3]
    d2 = (today < yest) and (yest < yest2)
    win5 = closes[-5:]
    hi, lo = max(win5), min(win5)
    rel = (today - lo) / (hi - lo) if hi > lo else 1.0
    r = rsi(closes, RSI_PERIOD)
    ret20 = today / closes[-21] - 1.0
    passes = (
        d2
        and rel <= DEPTH_OF_PB
        and (r is not None and r < RSI_MAX)
        and ret20 > CRASH_GUARD
    )
    return passes, {
        "down_2_days": d2,
        "rel_in_5d":   rel,
        "rsi":         r,
        "ret_20d":     ret20,
        "today":       today,
        "yest":        yest,
    }


def company_stem(name):
    """Strip share-class / form suffix so A and B of the same company collapse."""
    for s in (" SDB", " Pref", " A", " B", " C", " D", " R"):
        if name.endswith(s):
            return name[:-len(s)].rstrip()
    return name


def is_market_open(now_local=None):
    if now_local is None:
        now_local = datetime.now()
    if now_local.weekday() >= 5:
        return False
    mins = now_local.hour * 60 + now_local.minute
    return 9 * 60 <= mins <= 17 * 60 + 30


# ----------------------------------------------------------------------------- universe

def _find_history_csv(insref):
    """Return (folder_name, csv_path) for an insref, or (None, None) if missing."""
    matches = glob.glob(os.path.join(BASE, f"{insref}_*"))
    if not matches:
        return None, None
    folder = matches[0]
    hist = glob.glob(os.path.join(folder, "*_History.csv"))
    if not hist:
        return os.path.basename(folder), None
    return os.path.basename(folder), hist[0]


def build_universe():
    """
    Returns (primary, watchlist) where each is a list of dicts:
      {name, insref, folder, csv, tier}
    primary = large cap, watchlist = mid/small cap.
    Filters out tickers with no history CSV or <30 rows.
    """
    primary, watchlist = [], []
    if not os.path.exists(NAMES_CSV):
        return primary, watchlist

    with open(NAMES_CSV, newline="", encoding="utf-8") as f:
        for row in csv.reader(f):
            if len(row) < 4:
                continue
            insref_raw, name, tier = row[0], row[1], row[-1].strip()
            try:
                insref = int(insref_raw)
            except ValueError:
                continue
            if tier not in ("large cap", "mid cap", "small cap"):
                continue
            folder, csv_path = _find_history_csv(insref)
            if not csv_path:
                continue
            # Quick row count check
            with open(csv_path, encoding="utf-8") as fh:
                n_rows = sum(1 for _ in fh)
            if n_rows < MIN_HISTORY_DAYS:
                continue
            entry = {
                "name":   name,
                "insref": insref,
                "folder": folder,
                "csv":    csv_path,
                "tier":   tier,
            }
            (primary if tier == "large cap" else watchlist).append(entry)
    return primary, watchlist


def get_live(insref):
    q = infoScrapper.fetch_quote(insref)
    if isinstance(q, dict) and "error" not in q:
        return {
            "last":    q.get("lastprice"),
            "bid":     q.get("bidprice"),
            "ask":     q.get("askprice"),
            "diffprc": q.get("diff1dprc"),
            "ok":      True,
        }
    return {"ok": False, "err": (q.get("error") if isinstance(q, dict) else "unknown")}


def fetch_live_parallel(tickers):
    """tickers: iterable of dicts with 'name' and 'insref'."""
    out = {}
    with ThreadPoolExecutor(max_workers=LIVE_FETCH_WORKERS) as ex:
        futs = {ex.submit(get_live, t["insref"]): t["name"] for t in tickers}
        for fut in as_completed(futs):
            out[futs[fut]] = fut.result()
    return out


# ----------------------------------------------------------------------------- main

def main():
    state = load_state()
    now = datetime.now(timezone.utc)
    today = now.date()

    print("=" * 72)
    print(f" Live signal run @ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} local")
    print("=" * 72)

    if not is_market_open():
        print(" Market closed (Stockholm OMX 09:00-17:30 Mon-Fri). Skipping.")
        return

    # ---------- 1. Build universe + log changes
    primary, watchlist = build_universe()
    primary_names = sorted(t["name"] for t in primary)
    watchlist_names = sorted(t["name"] for t in watchlist)
    prev = set(state.get("last_universe", []))
    cur  = set(primary_names)
    added   = sorted(cur - prev)
    removed = sorted(prev - cur)

    print(f" Universe: {len(primary)} large cap (tradable)  |  "
          f"{len(watchlist)} mid+small cap (watchlist only)")
    if added:
        print(f"   + added to primary:   {', '.join(added)}")
    if removed:
        print(f"   - removed from primary: {', '.join(removed)}")

    # ---------- 2. Live quotes for primary universe (parallel)
    primary_by_name = {t["name"]: t for t in primary}
    live = fetch_live_parallel(primary)
    for name, t in primary_by_name.items():
        lq = live.get(name, {"ok": False})
        if not lq.get("ok") or lq.get("last") in (None, 0):
            rows = load_history_closes(t["csv"])
            if rows:
                lq = {"last": rows[-1][1], "ask": rows[-1][1], "bid": rows[-1][1],
                      "ok": True, "source": "csv-fallback"}
        else:
            lq["source"] = "live"
        live[name] = lq

    # Also fetch live for any open positions whose ticker fell out of universe
    for tk in state["positions"]:
        if tk in live:
            continue
        # Find this ticker's insref from the position itself (we stored it on entry)
        insref = state["positions"][tk].get("insref")
        if insref:
            live[tk] = get_live(insref)
            if not live[tk].get("ok"):
                live[tk] = {"last": state["positions"][tk]["entry_price"], "ok": True,
                            "source": "stale"}
            else:
                live[tk]["source"] = "live"

    # ---------- 3. Exit checks on open positions
    closed_now = []
    for tk in list(state["positions"].keys()):
        pos = state["positions"][tk]
        cur_price = live.get(tk, {}).get("last")
        if cur_price is None:
            continue
        target_date = (datetime.fromisoformat(pos["target_exit_date"]).date()
                       if isinstance(pos["target_exit_date"], str)
                       else pos["target_exit_date"])

        exit_reason = None
        if cur_price >= pos["tp"]:
            exit_reason = "TP"
        elif cur_price <= pos["sl"]:
            exit_reason = "SL"
        elif today >= target_date:
            exit_reason = "TIMEOUT"

        if exit_reason:
            proceeds = pos["shares"] * cur_price
            pnl = proceeds - pos["shares"] * pos["entry_price"]
            state["cash"] += proceeds
            closed = {
                "ticker":      tk,
                "entry_date":  pos["entry_date"],
                "exit_date":   today.isoformat(),
                "entry_price": pos["entry_price"],
                "exit_price":  cur_price,
                "shares":      pos["shares"],
                "pnl_sek":     pnl,
                "ret_pct":     cur_price / pos["entry_price"] - 1.0,
                "reason":      exit_reason,
            }
            state["closed_trades"].append(closed)
            closed_now.append(closed)
            del state["positions"][tk]

    # ---------- 4. Scan primary universe — first gather candidates, then rank
    candidates = []
    primary_filtered_info = []
    for t in primary:
        name = t["name"]
        if name in state["positions"]:
            continue
        lq = live.get(name)
        if not lq or lq.get("last") is None:
            continue
        rows = load_history_closes(t["csv"])
        if len(rows) < 30:
            continue

        last_row_d = datetime.fromtimestamp(rows[-1][0] / 1000, tz=timezone.utc).date()
        live_close = lq["last"]
        closes = ([r[1] for r in rows[:-1]] + [live_close]) if last_row_d == today \
                 else ([r[1] for r in rows] + [live_close])

        ok, info = signal_check(closes)
        if not ok:
            if "err" not in info:
                primary_filtered_info.append((name, info))
            continue
        candidates.append({"t": t, "lq": lq, "info": info, "live_close": live_close})

    # Rank: strongest signal first (lowest RSI)
    candidates.sort(key=lambda c: (c["info"]["rsi"] if c["info"]["rsi"] is not None else 100))

    # Dedupe by company stem so we don't take A and B of the same name
    held_stems = {company_stem(tk) for tk in state["positions"]}
    new_signals = []
    for c in candidates:
        t, lq, info, live_close = c["t"], c["lq"], c["info"], c["live_close"]
        name = t["name"]
        stem = company_stem(name)
        if stem in held_stems:
            new_signals.append({"ticker": name, "skipped": f"duplicate of held '{stem}'"})
            continue
        if state["cash"] < POS_MIN:
            new_signals.append({"ticker": name, "skipped": f"low cash {state['cash']:.0f}"})
            continue
        entry_price = lq.get("ask") or live_close
        size = min(POS_TARGET, state["cash"], POS_MAX)
        shares = int(size // entry_price)
        if shares <= 0 or shares * entry_price < POS_MIN:
            new_signals.append({"ticker": name, "skipped": "size <50k after rounding"})
            continue

        cost     = shares * entry_price
        tp_price = round(entry_price * (1 + TP_PCT), 4)
        sl_price = round(entry_price * (1 + SL_PCT), 4)
        exit_by  = add_trading_days(today, MAX_HOLD_DAYS)

        state["cash"] -= cost
        state["positions"][name] = {
            "insref":           t["insref"],
            "entry_price":      entry_price,
            "shares":           shares,
            "entry_date":       today.isoformat(),
            "tp":               tp_price,
            "sl":               sl_price,
            "target_exit_date": exit_by.isoformat(),
            "cost_sek":         cost,
            "signal_info":      info,
        }
        held_stems.add(stem)
        new_signals.append({
            "ticker":   name,
            "entry":    entry_price,
            "tp":       tp_price,
            "sl":       sl_price,
            "shares":   shares,
            "cost":     cost,
            "exit_by":  exit_by.isoformat(),
            "info":     info,
        })

    # ---------- 5. Watchlist scan (mid/small cap, CSV-only, no entries)
    watchlist_hits = []
    for t in watchlist:
        rows = load_history_closes(t["csv"])
        if len(rows) < 30:
            continue
        closes = [r[1] for r in rows]
        ok, info = signal_check(closes)
        if ok:
            watchlist_hits.append({
                "ticker": t["name"],
                "last":   closes[-1],
                "info":   info,
            })

    # ---------- 6. Reporting
    open_value = 0.0
    for tk, pos in state["positions"].items():
        cur_price = live.get(tk, {}).get("last") or pos["entry_price"]
        open_value += pos["shares"] * cur_price
    equity = state["cash"] + open_value
    pnl_realized = sum(t["pnl_sek"] for t in state["closed_trades"])

    print(f"\nWallet:    cash {state['cash']:>10,.0f}   open {open_value:>10,.0f}   "
          f"equity {equity:>10,.0f}  SEK")
    print(f"P&L:       realized {pnl_realized:+,.0f} SEK   "
          f"open positions {len(state['positions'])}   "
          f"closed trades {len(state['closed_trades'])}")

    if closed_now:
        print(f"\n--- CLOSED THIS RUN ({len(closed_now)}) ---")
        for t in closed_now:
            print(f"  {t['ticker']:<22} {t['reason']:<7} @ {t['exit_price']:.2f}   "
                  f"P&L {t['pnl_sek']:+,.0f} SEK ({t['ret_pct']*100:+.2f}%)   "
                  f"opened {t['entry_date']}")

    entered = [s for s in new_signals if "skipped" not in s]
    skipped = [s for s in new_signals if "skipped" in s]
    if entered or skipped:
        print(f"\n--- NEW SIGNALS ({len(entered)} entered, {len(skipped)} skipped) ---")
        for s in entered:
            print(f"  BUY {s['ticker']:<18}  {s['shares']:>5} sh @ {s['entry']:.2f}  "
                  f"= {s['cost']:>9,.0f} SEK")
            print(f"      target exit {s['tp']:.2f} (+0.50%)   "
                  f"stop {s['sl']:.2f} (-2.00%)   "
                  f"exit by {s['exit_by']}")
            i = s["info"]
            print(f"      signal:  RSI {i['rsi']:.1f}   "
                  f"bottom {i['rel_in_5d']*100:.0f}% of 5d range   "
                  f"20d ret {i['ret_20d']*100:+.1f}%")
        for s in skipped[:3]:
            print(f"  skipped: {s['ticker']:<22} ({s['skipped']})")
        if len(skipped) > 3:
            print(f"  ... +{len(skipped) - 3} more skipped")
    else:
        print("\n--- NO NEW SIGNALS IN PRIMARY UNIVERSE ---")

    if watchlist_hits:
        print(f"\n--- WATCHLIST SIGNALS ({len(watchlist_hits)}) — monitor only, do NOT trade ---")
        for w in watchlist_hits[:20]:
            i = w["info"]
            print(f"  {w['ticker']:<28} last {w['last']:>7.2f}   "
                  f"RSI {i['rsi']:.1f}   "
                  f"bot {i['rel_in_5d']*100:.0f}%   "
                  f"20d {i['ret_20d']*100:+.1f}%")
        if len(watchlist_hits) > 20:
            print(f"  ... +{len(watchlist_hits) - 20} more")

    if state["positions"]:
        print(f"\n--- OPEN POSITIONS ({len(state['positions'])}) ---")
        for tk, pos in state["positions"].items():
            cur_price = live.get(tk, {}).get("last") or pos["entry_price"]
            pnl = pos["shares"] * (cur_price - pos["entry_price"])
            ret = cur_price / pos["entry_price"] - 1
            print(f"  {tk:<22} {pos['shares']:>5} sh   "
                  f"entered {pos['entry_date']} @ {pos['entry_price']:.2f}   "
                  f"now {cur_price:.2f}   P&L {pnl:+,.0f} SEK ({ret*100:+.2f}%)")
            print(f"                          TP {pos['tp']:.2f}   SL {pos['sl']:.2f}   "
                  f"exit by {pos['target_exit_date']}")

    # Compact diagnostic: closest-to-passing in primary universe
    def closeness(info):
        # smaller = closer to passing
        rsi_gap   = max(0, (info["rsi"] - RSI_MAX) / RSI_MAX) if info["rsi"] else 1
        depth_gap = max(0, info["rel_in_5d"] - DEPTH_OF_PB)
        d2_gap    = 0.0 if info["down_2_days"] else 0.5
        crash_gap = max(0, CRASH_GUARD - info["ret_20d"])
        return rsi_gap + depth_gap + d2_gap + crash_gap

    if primary_filtered_info:
        ranked = sorted(primary_filtered_info, key=lambda x: closeness(x[1]))[:5]
        print(f"\n--- CLOSEST TO SIGNAL (top 5 of {len(primary_filtered_info)} primary) ---")
        for name, info in ranked:
            checks = []
            if not info["down_2_days"]:                checks.append("not down-2")
            if info["rel_in_5d"] > DEPTH_OF_PB:        checks.append(f"5d:{info['rel_in_5d']*100:.0f}%")
            if info["rsi"] is None or info["rsi"] >= RSI_MAX:
                checks.append(f"RSI:{info['rsi']:.1f}" if info["rsi"] is not None else "RSI:?")
            if info["ret_20d"] <= CRASH_GUARD:         checks.append(f"20d:{info['ret_20d']*100:+.1f}%")
            print(f"  {name:<26} fails: {', '.join(checks) if checks else 'borderline'}")

    state["last_universe"] = primary_names
    state["last_watchlist"] = watchlist_names
    save_state(state)


if __name__ == "__main__":
    main()
