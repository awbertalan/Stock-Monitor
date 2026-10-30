"""Trade notifications — one message per fill, from the engine that filled it.

Two sinks, both optional and independently toggled:

  * **macOS banner** (``osascript``). Zero setup, but only useful while you are
    at this Mac. The engines run under a *user* LaunchAgent in the Aqua session,
    so a banner posted from a launchd tick does reach Notification Centre —
    with one caveat: the posting app is Script Editor, so notifications must be
    allowed for **Script Editor** in System Settings → Notifications, or macOS
    drops them silently. The Settings page has a Test button for exactly this.

  * **ntfy push** (https://ntfy.sh). Reaches the phone. Publishes with the JSON
    API rather than the header API on purpose: ntfy sends ``Title``/``Tags`` as
    HTTP headers, which are latin-1, and half the Stockholm tape is names like
    "Gränges" and "Hexpol" — a UTF-8 title in a header either mojibakes or
    raises. The JSON body is UTF-8 clean.

The topic name IS the credential — anyone who knows it can both read your
trades and publish to your phone — so it lives in ``.env`` (gitignored, and
never served by app.py) and not in ``state/settings.json``, which the web app
hands to any client that can reach it.

Delivery model
--------------
Events are queued during a run and flushed once, after the wallet has been
saved. That ordering is deliberate: a crash between the fill and the flush
loses a notification, whereas notifying first and crashing before the save
would announce a trade that never happened. Losing one is recoverable by
looking at the page; inventing one is not.

Call order alone is not proof of that, though: the queue is one module global
and all three engines run in the SAME process (live_signals' ``__main__``
chains patterns then cross and swallows their exceptions). A crash between
live_patterns' fill and its save used to leave the sell sitting in the queue
for live_cross' flush to announce — with the wallet on disk still showing the
position open. So every fill carries the moment it was queued, and ``flush()``
sends it only once that engine's wallet file is newer than that: the save on
disk is the evidence. Events that cannot be confirmed are held for a later
flush in the same run and, at interpreter exit, dropped with a log line
naming them rather than announced.

``flush()`` is idempotent (it drains the queue) and also registered with
``atexit``, so the engines' many early-return paths (the EOD sweep sells and
returns long before the flush at the end of main) cannot strand events.

Everything here fails soft. A notification must never be the reason a trade
does not get booked: every send is wrapped, the network sink has a 4-second
timeout and the whole flush has a wall-clock budget, after which the remainder
is dropped with a log line rather than delaying the next engine tick.
"""

import atexit
import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
SETTINGS_PATH = os.path.join(_ROOT, "state", "settings.json")

# Per-send network timeout, and the ceiling on one whole flush. The engines tick
# every 180s; 17 fills in one pass (the 2026-08-24 record) must not be able to
# push a run into the next one.
SEND_TIMEOUT_S = 4
FLUSH_BUDGET_S = 30

DEFAULTS = {
    "enabled":      True,
    "macos":        True,
    "macos_sound":  True,
    "ntfy":         True,        # inert until NTFY_TOPIC is set in .env
    "ntfy_server":  "https://ntfy.sh",
}

# Exit reasons -> (macOS-safe glyph, ntfy tag). ntfy renders tags as emoji from
# its own shortcode list, so the two are not interchangeable.
_REASON_ICON = {
    "TP":      ("🟢", "white_check_mark"),
    "SL":      ("🔴", "octagonal_sign"),
    "TIMEOUT": ("⏱",  "hourglass"),
    "MANUAL":  ("👤", "wave"),
    "STALE":   ("⚠️",  "warning"),
}
_BUY_ICON = ("🔵", "chart_with_upwards_trend")

ENGINE_LABELS = {"signals": "Signals", "patterns": "Patterns", "cross": "Cross",
                 "breakout": "Breakout"}

# The wallet each engine writes, mirroring STATE_PATH in live_signals.py,
# live_patterns.py, live_cross.py and live_breakout.py. Repeated here rather than imported
# because those modules import this one; a name that ever drifts surfaces as
# the "no wallet mapped" warning in flush(), not as a silent hole.
_ENGINE_WALLET = {
    "signals":  os.path.join(_ROOT, "state", "live_portfolio.json"),
    "patterns": os.path.join(_ROOT, "state", "live_patterns_portfolio.json"),
    "cross":    os.path.join(_ROOT, "state", "live_cross_portfolio.json"),
    "breakout": os.path.join(_ROOT, "state", "live_breakout_portfolio.json"),
}

_queue = []
_lock = threading.Lock()
_atexit_registered = False
_in_atexit = False
_warned_engines = set()


# ── config ───────────────────────────────────────────────────────────────────

def config():
    """The ``notifications`` block of settings.json, over the defaults.

    Read on every flush rather than cached: the Settings page can toggle a sink
    between two engine ticks, and an engine process is short-lived anyway."""
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            block = (json.load(f) or {}).get("notifications") or {}
    except (OSError, json.JSONDecodeError):
        block = {}
    return {**DEFAULTS, **block}


def ntfy_topic():
    """The topic from the environment, or None. env_loader must have run."""
    topic = (os.environ.get("NTFY_TOPIC") or "").strip()
    return topic or None


def status():
    """What is actually wired up — for the Settings page and the run log."""
    cfg = config()
    topic = ntfy_topic()
    return {
        "enabled":       bool(cfg["enabled"]),
        "macos":         bool(cfg["macos"]),
        "ntfy":          bool(cfg["ntfy"]),
        "ntfy_ready":    bool(cfg["ntfy"] and topic),
        "ntfy_server":   cfg["ntfy_server"],
        # Never the topic itself: /get-settings is reachable by anything that
        # can reach the web app. Enough to tell "configured" from "not".
        "ntfy_topic_set": bool(topic),
        "sinks": [s for s, on in (("macOS", cfg["macos"]),
                                  ("ntfy", cfg["ntfy"] and topic)) if on],
    }


# ── sinks ────────────────────────────────────────────────────────────────────

def _osa_quote(s):
    """AppleScript string literal — only \\ and " need escaping, and newlines
    have to become the escape sequence rather than a raw break."""
    return (str(s).replace("\\", "\\\\").replace('"', '\\"')
            .replace("\n", "\\n").replace("\r", ""))


def _send_macos(title, subtitle, body, sound):
    script = (f'display notification "{_osa_quote(body)}" '
              f'with title "{_osa_quote(title)}"')
    if subtitle:
        script += f' subtitle "{_osa_quote(subtitle)}"'
    if sound:
        script += ' sound name "Ping"'
    subprocess.run(["osascript", "-e", script],
                   check=True, capture_output=True, timeout=SEND_TIMEOUT_S)


def _send_ntfy(server, topic, title, body, tag, priority):
    payload = json.dumps({
        "topic":    topic,
        "title":    title,
        "message":  body,
        "tags":     [tag] if tag else [],
        "priority": priority,
    }).encode("utf-8")
    req = urllib.request.Request(
        server.rstrip("/") + "/",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST")
    token = (os.environ.get("NTFY_TOKEN") or "").strip()
    if token:                      # only needed for a private / self-hosted server
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=SEND_TIMEOUT_S) as r:
        r.read()


# ── message construction ─────────────────────────────────────────────────────

def _fmt(n, d=2):
    """Swedish grouping, matching the web UI ('1 234,50')."""
    try:
        s = f"{float(n):,.{d}f}"
    except (TypeError, ValueError):
        return "—"
    return s.replace(",", "\u00a0").replace(".", ",")


def _sek(n):
    try:
        return f"{'+' if n >= 0 else '−'}{_fmt(abs(n), 0)} SEK"
    except (TypeError, ValueError):
        return "—"


# Both builders return (title, lines, tag, priority). The two sinks lay the
# same lines out differently: macOS gets line 1 as the subtitle and the rest as
# the body (its banner gives the subtitle more room than the body), while ntfy
# takes every line as the message, since a phone's notification list shows the
# title and the first message line together.
def _buy_message(e):
    label = ENGINE_LABELS.get(e["engine"], e["engine"])
    glyph, tag = _BUY_ICON
    title = f"{glyph} {label} bought {e['ticker']}"
    lines = [f"{_fmt(e['shares'], 0)} sh @ {_fmt(e['entry_price'])}"
             f"  =  {_fmt(e.get('cost') or 0, 0)} SEK"]
    if e.get("tp") and e.get("sl"):
        lines.append(f"TP {_fmt(e['tp'])} / SL {_fmt(e['sl'])}"
                     + (f"  ·  exit by {e['exit_by']}" if e.get("exit_by") else ""))
    elif e.get("exit_by"):
        lines.append(f"Exit by {e['exit_by']}")
    if e.get("pattern"):
        lines.append(str(e["pattern"]))
    return title, lines, tag, 3


def _sell_message(e):
    label = ENGINE_LABELS.get(e["engine"], e["engine"])
    reason = e.get("reason") or ""
    glyph, tag = _REASON_ICON.get(reason, ("⚪️", "bell"))
    ret = e.get("ret_pct")
    ret_s = f"{'+' if (ret or 0) >= 0 else ''}{(ret or 0) * 100:.2f}%"
    title = f"{glyph} {label} sold {e['ticker']}"
    lines = [f"{ret_s}  ·  {reason}  ·  {_sek(e.get('pnl_sek'))}",
             f"{_fmt(e['shares'], 0)} sh  {_fmt(e['entry_price'])} → {_fmt(e['exit_price'])}"
             f"  ·  held {e.get('hold') or '—'}"]
    # A stop-out is the one event worth a louder push than a take-profit.
    priority = 4 if reason == "SL" else 3
    return title, lines, tag, priority


# ── public API ───────────────────────────────────────────────────────────────

def _flush_at_exit():
    """The last flush of the process.

    atexit fires on the clean early-return paths the engines rely on, but it
    also fires during interpreter shutdown after an UNHANDLED exception — when
    the wallet on disk is exactly as it was before the run. Marking the flush
    so it knows this is the final call is what lets it drop the unconfirmed
    events loudly instead of holding them for a flush that will never come."""
    global _in_atexit
    _in_atexit = True
    flush()


def _register_atexit():
    global _atexit_registered
    if not _atexit_registered:
        atexit.register(_flush_at_exit)
        _atexit_registered = True


def queue_buy(engine, ticker, entry_price, shares, cost=None,
              tp=None, sl=None, exit_by=None, pattern=None):
    with _lock:
        _queue.append({"kind": "buy", "engine": engine, "ticker": ticker,
                       "entry_price": entry_price, "shares": shares, "cost": cost,
                       "tp": tp, "sl": sl, "exit_by": exit_by, "pattern": pattern,
                       "queued_at": time.time()})
    _register_atexit()


def queue_message(title, lines, tag="bell", priority=3):
    """A message that is neither a fill nor an exit — a status snapshot, a test.

    Kept separate from queue_buy/queue_sell on purpose: those two shapes ARE the
    contract ('bought'/'sold' in the title means money moved), and a snapshot
    that borrowed them would be indistinguishable from a real fill on the phone.
    Callers title these themselves."""
    with _lock:
        _queue.append({"kind": "message", "title": title,
                       "lines": [str(l) for l in lines if l],
                       "tag": tag, "priority": int(priority)})
    _register_atexit()


def queue_sell(engine, closed, hold=None):
    """`closed` is the trade dict apply_exit_checks() just appended."""
    with _lock:
        _queue.append({"kind": "sell", "engine": engine,
                       "ticker":      closed.get("ticker"),
                       "entry_price": closed.get("entry_price"),
                       "exit_price":  closed.get("exit_price"),
                       "shares":      closed.get("shares"),
                       "pnl_sek":     closed.get("pnl_sek"),
                       "ret_pct":     closed.get("ret_pct"),
                       "reason":      closed.get("reason"),
                       "hold":        hold,
                       # Stamped so flush() can ask whether the wallet was
                       # saved AFTER this exit was booked — see _unconfirmed().
                       "queued_at":   time.time()})
    _register_atexit()


def pending():
    with _lock:
        return len(_queue)


def _unconfirmed(e, out):
    """Why this event must not be announced yet, or None when it is safe.

    The proof that a fill really happened is the engine's wallet on disk being
    newer than the moment the fill was queued. Flushing after save_state() was
    meant to guarantee that, but _queue is one global shared by all three
    engines in one process: when live_patterns crashed between its exit and its
    save, live_cross' flush announced the sell — into live_cross.log, under the
    wrong engine's name — while the position sat open in the wallet, ready to
    stop out again three minutes later at a different price."""
    if e.get("kind") == "message" or e.get("test"):
        return None                # a snapshot or a specimen — no wallet backs it
    queued_at = e.get("queued_at")
    if queued_at is None:
        return None                # queued by something older than the stamp
    engine = e.get("engine")
    path = _ENGINE_WALLET.get(engine)
    if not path:
        # A new or renamed engine must not silently lose its notifications, but
        # losing the guard has to be visible rather than assumed — a gate that
        # fails quietly is itself a bug.
        if engine not in _warned_engines:
            _warned_engines.add(engine)
            out(f"   ⚠ notifications: no wallet mapped for engine {engine!r} — "
                f"cannot confirm it was saved, sending anyway")
        return None
    try:
        saved_at = os.path.getmtime(path)
    except OSError:
        return "wallet has never been written"
    if saved_at < queued_at:
        # Compared straight, with no slack: in every real path the gap between
        # queueing a fill and saving the wallet is filled with network work, so
        # the save always lands later. Slack would only widen the window in
        # which an event queued just after some EARLIER save — and then lost to
        # a crash — is waved through as if it had been persisted.
        return "wallet not saved since the fill"
    return None


def _flush(out):
    """flush() with the per-sink detail kept.

    flush() narrows the result to the (sent, failed, dropped) tuple its callers
    unpack; send_test() needs to know WHICH sink refused, because "one of them
    worked" is not an answer to "did it reach my phone"."""
    with _lock:
        events, _queue[:] = list(_queue), []
    empty = {"sent": 0, "failed": 0, "dropped": 0, "by_sink": {}}
    if not events:
        return empty

    cfg = config()
    if not cfg["enabled"]:
        return {**empty, "dropped": len(events)}

    topic = ntfy_topic()
    want_macos = bool(cfg["macos"])
    want_ntfy = bool(cfg["ntfy"]) and bool(topic)
    if not (want_macos or want_ntfy):
        # ntfy switched on but no topic in .env is the one silent-failure case
        # worth naming in the log — see feedback on engine gates surfacing.
        if cfg["ntfy"] and not topic:
            out("   ⚠ notifications: ntfy enabled but NTFY_TOPIC is unset in .env — nothing sent")
        return {**empty, "dropped": len(events)}

    dropped = 0
    ready, held = [], []
    for e in events:
        (held if _unconfirmed(e, out) else ready).append(e)
    if held:
        if _in_atexit:
            # Last call of the process, so the wallet is never going to be
            # written: these fills were rolled back by the crash. Name them —
            # a dropped notification the owner knows about is recoverable from
            # the page; an invented one is not.
            names = ", ".join(str(e.get("ticker") or e.get("engine") or "?") for e in held)
            out(f"   ⚠ notifications: {len(held)} queued event(s) DROPPED — the wallet "
                f"was never saved, so the trade did not happen ({names})")
            dropped += len(held)
        else:
            with _lock:
                _queue[:0] = held    # a later flush this run may still confirm them
    events = ready

    by_sink = {}
    if want_macos:
        by_sink["macOS"] = {"sent": 0, "failed": 0}
    if want_ntfy:
        by_sink["ntfy"] = {"sent": 0, "failed": 0}

    sent = failed = 0
    started = time.monotonic()
    for i, e in enumerate(events):
        if time.monotonic() - started > FLUSH_BUDGET_S:
            n = len(events) - i
            dropped += n
            out(f"   ⚠ notifications: {FLUSH_BUDGET_S}s budget spent, dropped {n}")
            break
        try:
            if e["kind"] == "message":
                title, lines = e["title"], e["lines"]
                tag, priority = e["tag"], e["priority"]
            else:
                build = _buy_message if e["kind"] == "buy" else _sell_message
                title, lines, tag, priority = build(e)
            subtitle, body = lines[0], "\n".join(lines[1:])
        except Exception as exc:                       # noqa: BLE001 - never fatal
            out(f"   ⚠ notifications: could not format {e.get('ticker')}: {exc}")
            failed += 1
            continue
        missed = 0
        if want_macos:
            try:
                _send_macos(title, subtitle, body, cfg["macos_sound"])
                by_sink["macOS"]["sent"] += 1
            except (subprocess.SubprocessError, OSError) as exc:
                by_sink["macOS"]["failed"] += 1
                missed += 1
                out(f"   ⚠ notifications: macOS banner failed for {e.get('ticker')}: {exc}")
        if want_ntfy:
            try:
                _send_ntfy(cfg["ntfy_server"], topic, title, "\n".join(lines),
                           tag, priority)
                by_sink["ntfy"]["sent"] += 1
            except (urllib.error.URLError, OSError, ValueError) as exc:
                by_sink["ntfy"]["failed"] += 1
                missed += 1
                out(f"   ⚠ notifications: ntfy failed for {e.get('ticker')}: {exc}")
        # An event counts as sent when ANY enabled sink took it. Deliberately
        # an OR: this tally answers "did the notification get out at all", and
        # app.py's health watchdog advances its "already alerted" mark only
        # when failed == 0. Counting a delivered banner as a failure because
        # ntfy happened to be unreachable would re-queue and re-send the same
        # health alert every 15 minutes for as long as ntfy stayed down. The
        # stricter "every sink took it" question belongs to the Settings Test
        # button, and by_sink answers it there without moving this number.
        if missed < len(by_sink):
            sent += 1
        else:
            failed += 1
    if sent or failed or dropped:
        detail = ", ".join(f"{name} {t['sent']}/{t['sent'] + t['failed']}"
                           for name, t in by_sink.items())
        out(f"   🔔 notifications: {sent} sent, {failed} failed"
            + (f", {dropped} dropped" if dropped else "")
            + f" ({detail or 'nothing'})")
    return {"sent": sent, "failed": failed, "dropped": dropped, "by_sink": by_sink}


def flush(out=print):
    """Send everything queued whose wallet is on disk. Returns (sent, failed, dropped).

    Drains the queue first, so a second call (atexit after an explicit one) is
    a no-op rather than a duplicate send. `sent` counts events that reached at
    least one enabled sink and `failed` the ones no sink took — the question
    app.py's watchdog retries on."""
    r = _flush(out)
    return (r["sent"], r["failed"], r["dropped"])


def send_test(out=print):
    """Fire one specimen of each kind through every enabled sink.

    Used by the Settings page's Test button: the macOS sink fails *silently*
    when Script Editor is not allowed in System Settings → Notifications, so
    "did it work" can only be answered by looking."""
    with _lock:
        first = len(_queue)
    queue_buy("signals", "Test AB", 100.0, 100, cost=10_000,
              tp=103.0, sl=99.0, exit_by="2026-01-01", pattern="Notification test")
    queue_sell("patterns", {
        "ticker": "Test AB", "entry_price": 100.0, "exit_price": 103.0,
        "shares": 100, "pnl_sek": 300.0, "ret_pct": 0.03, "reason": "TP",
    }, hold="1d")
    # Specimens, not fills: no wallet was written for them, so they are exempt
    # from the save-confirmation gate flush() applies to real events — without
    # this the Test button would silently send nothing at all.
    with _lock:
        for e in _queue[first:]:
            e["test"] = True
    r = _flush(out)
    st = status()
    # `ok` needs every ENABLED sink to have taken both specimens: a test that
    # only proves the Mac banner works answers the wrong question. It is
    # computed from by_sink so the strictness stays HERE — `sent`/`failed` stay
    # the any-sink counts, because web/settings.html reads `!res.sent` as "no
    # sink is active" and a banner that actually arrived must not print that.
    by_sink = r["by_sink"]
    ok = bool(by_sink) and all(t["failed"] == 0 and t["sent"] > 0
                               for t in by_sink.values())
    return {"ok": ok,
            "sent": r["sent"], "failed": r["failed"], "dropped": r["dropped"],
            "by_sink": by_sink, **st}
