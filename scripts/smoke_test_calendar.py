"""
Smoke test: backtest the live_signals validated strategy on OMX large cap
over the window covered by events_calendar.json (Nov 2025 -> today), and
compare per-trade stats with vs. without the event-calendar filter applied.

History rows are daily closes only (no H/L), so TP/SL detection is close-based
(same as the live engine when no intraday touches are available). Per-ticker
cooldowns mirror the live rules.
"""
import os
import sys
import json
from datetime import date, datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "lib"))

import live_signals as L
import calendar_cache


WINDOW_START = date(2025, 11, 26)   # earliest event in cache
WINDOW_END   = date(2026, 5, 25)    # today

TP = L.TP_PCT
SL = L.SL_PCT
MAX_HOLD = L.MAX_HOLD_DAYS
COOLDOWN = L.COOLDOWN_DAYS


def simulate_one(closes_dates, entry_idx, entry_price):
    """Walk forward up to MAX_HOLD trading days, exiting on the first close
    that crosses TP or SL, else timeout. Returns (exit_idx, exit_price, ret, reason)."""
    days_held = 0
    for i in range(entry_idx + 1, len(closes_dates)):
        days_held += 1
        d, c = closes_dates[i]
        ret = c / entry_price - 1.0
        if ret >= TP:
            return i, c, ret, "TP"
        if ret <= SL:
            return i, c, ret, "SL"
        if days_held >= MAX_HOLD:
            return i, c, ret, "TIMEOUT"
    # Ran out of history -> mark unresolved
    return None, None, None, "OPEN"


def run_backtest(primary, calendar=None):
    """If calendar is None, no filter. Otherwise apply has_nearby_event."""
    trades = []
    blocked_by_calendar = 0
    for t in primary:
        rows = L.load_history_closes(t["csv"])
        # rows = list of (ts_ms, price)
        # Convert to (date, close)
        closes = [(datetime.fromtimestamp(ts / 1000).date(), p) for ts, p in rows]
        # Pre-build closes-only list for signal_check
        prices = [p for _, p in closes]

        cooldown_until = None  # date the ticker is blocked through (inclusive)
        last_exit_date = None  # for same-day block on TP/TIMEOUT

        for i in range(20, len(closes)):
            d, p = closes[i]
            if d < WINDOW_START or d > WINDOW_END:
                continue
            # Cooldown blocks
            if cooldown_until is not None and d <= cooldown_until:
                continue
            if last_exit_date is not None and d == last_exit_date:
                continue
            window = prices[: i + 1]
            ok, info = L.signal_check(window, today_date=d)
            if not ok:
                continue
            # Calendar filter (mirrors live engine call)
            if calendar is not None:
                blocked, _ = calendar_cache.has_nearby_event(
                    t["insref"], d, window_days=calendar_cache.WINDOW_DAYS,
                    cache=calendar,
                )
                if blocked:
                    blocked_by_calendar += 1
                    continue
            # Enter at this close
            exit_idx, exit_p, ret, reason = simulate_one(closes, i, p)
            if reason == "OPEN":
                continue
            exit_d = closes[exit_idx][0]
            trades.append({
                "ticker": t["name"],
                "entry_date": d.isoformat(),
                "entry_price": p,
                "exit_date": exit_d.isoformat(),
                "exit_price": exit_p,
                "ret": ret,
                "reason": reason,
                "dod": info["dod"],
                "ret_20d": info["ret_20d"],
            })
            # Apply cooldown rules (same as live engine)
            if reason == "SL":
                cooldown_until = exit_d + timedelta(days=COOLDOWN * 7 // 5)  # rough trading->calendar; live uses trading_days_between
                # Use trading-day arithmetic precisely:
                cooldown_until = L.add_trading_days(exit_d, COOLDOWN)
                last_exit_date = None
            else:
                cooldown_until = None
                last_exit_date = exit_d
    return trades, blocked_by_calendar


def stats(trades, label):
    if not trades:
        return f"{label}: 0 trades"
    n = len(trades)
    wins = sum(1 for t in trades if t["ret"] > 0)
    losses = n - wins
    wr = 100 * wins / n
    avg = sum(t["ret"] for t in trades) / n
    tp_n = sum(1 for t in trades if t["reason"] == "TP")
    sl_n = sum(1 for t in trades if t["reason"] == "SL")
    to_n = sum(1 for t in trades if t["reason"] == "TIMEOUT")
    # Total P&L on per-trade 70k notional (approx — ignores capital constraint)
    pnl_70k = sum(t["ret"] * 70_000 for t in trades)
    best = max(trades, key=lambda t: t["ret"])
    worst = min(trades, key=lambda t: t["ret"])
    return (
        f"{label}\n"
        f"  trades: {n}    wins: {wins} ({wr:.1f}%)    losses: {losses}\n"
        f"  TP/SL/TIMEOUT: {tp_n}/{sl_n}/{to_n}\n"
        f"  avg ret/trade: {avg*100:+.3f}%    sum-P&L @ 70k notional: {pnl_70k:+.0f} SEK\n"
        f"  best:  {best['ticker']:<22} {best['entry_date']} -> {best['exit_date']}  {best['ret']*100:+.2f}%\n"
        f"  worst: {worst['ticker']:<22} {worst['entry_date']} -> {worst['exit_date']}  {worst['ret']*100:+.2f}%"
    )


def main():
    primary, _ = L.build_universe()
    print(f"Large-cap universe: {len(primary)} tickers")
    print(f"Backtest window:    {WINDOW_START} -> {WINDOW_END}")
    print(f"Strategy:           TP +{TP*100:.1f}% / SL {SL*100:.1f}% / {MAX_HOLD}d hold / cooldown {COOLDOWN}d after SL")
    print()

    cal = calendar_cache.load_cache()
    print(f"Calendar cache:     {sum(len(v) for v in cal['events'].values())} events across {len(cal['events'])} stocks")
    print(f"Cache fetched_at:   {cal.get('fetched_at')}")
    print()

    print("Running WITHOUT calendar filter...")
    t_no, _ = run_backtest(primary, calendar=None)
    print("Running WITH calendar filter (±7 day window)...")
    t_yes, blocked = run_backtest(primary, calendar=cal)
    print()

    print(stats(t_no,  "BASELINE (no calendar filter)"))
    print()
    print(stats(t_yes, f"FILTERED (calendar filter, blocked {blocked} candidate entries)"))
    print()

    # Marginal effect of the filter — the trades that the filter actually removed
    keys_yes = {(t["ticker"], t["entry_date"]) for t in t_yes}
    removed = [t for t in t_no if (t["ticker"], t["entry_date"]) not in keys_yes]
    print(stats(removed, "TRADES REMOVED BY FILTER"))
    print()
    # Per-ticker net effect
    print("--- Tickers most filtered (top 8 by removed-trade count) ---")
    from collections import Counter
    cnt = Counter(t["ticker"] for t in removed)
    for ticker, c in cnt.most_common(8):
        ret_sum = sum(x["ret"] for x in removed if x["ticker"] == ticker)
        print(f"  {ticker:<25} removed: {c}  sum-ret: {ret_sum*100:+.2f}%")

    # Save trades for inspection
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "smoke_test_results.json"), "w") as f:
        json.dump({
            "window_start": WINDOW_START.isoformat(),
            "window_end":   WINDOW_END.isoformat(),
            "baseline": t_no,
            "filtered": t_yes,
            "removed":  removed,
        }, f, indent=2)
    print()
    print("Detailed results saved to smoke_test_results.json")


if __name__ == "__main__":
    main()
