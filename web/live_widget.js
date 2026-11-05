// live_widget.js — floating left-side summary of all four Live engines.
// Renders on every page except /live*  (the user is already there).
// Hidden on viewports narrower than 1360px (see style.css) so it never
// overlaps the centred page content.
//
// Every stock it lists — open positions and recent trades — opens a detail
// window on a plain click: the exact entry price, the planned exit (take-
// profit, stop-loss, exit-by date) and what the engine measured when it
// bought. The rows are still links, so Cmd/Ctrl-click or middle-click opens
// the stock page in a new tab as before.
//
// On a stock page it also fills the "Live trades" rail (#live-trades in
// stock_template.html): every trade any of the four engines has made in that
// stock, from the same wallets on the same poll, with each row opening the
// same detail window.

(function () {
  // Self-suppress on the live pages themselves
  const p = location.pathname;
  if (p === '/live' || p === '/live-overview' || p.startsWith('/live-')) return;

  const $ = (tag, attrs = {}, html = '') => {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === 'class') el.className = v;
      else if (k === 'style') el.style.cssText = v;
      else el.setAttribute(k, v);
    }
    if (html) el.innerHTML = html;
    return el;
  };

  const fmt = (n, d = 0) => Number(n).toLocaleString('sv-SE', { maximumFractionDigits: d, minimumFractionDigits: d });
  const sign = n => n >= 0 ? `+${fmt(n, 0)}` : fmt(n, 0);
  const cls = n => n > 0 ? 'up' : (n < 0 ? 'down' : '');
  // live_common.js is not loaded on the pages this widget lives on, so it
  // carries its own escaper. Anything read from a wallet (tickers, news
  // headlines, ledger text) goes through it before reaching innerHTML.
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  // Build the widget shell once
  const root = $('aside', { class: 'live-widget', id: 'live-widget' });
  root.innerHTML = `
    <div class="lw-head">
      <span class="lw-title">Live</span>
      <a class="lw-open" href="/live" title="Open Live Overview">↗</a>
    </div>
    <div class="lw-body" id="live-widget-body">
      <div class="lw-loading">Loading…</div>
    </div>
  `;
  // Defer DOM insertion until <body> is parsed
  function attach() {
    document.body.appendChild(root);
    root.addEventListener('click', onRowClick);
    const rail = document.getElementById('live-trades');   // stock pages only
    if (rail) rail.addEventListener('click', onRowClick);
    refresh();
    setInterval(refresh, 60_000);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', attach);
  else attach();

  // A failed wallet fetch used to be swallowed into `{}`, which summarise()
  // then read as a perfectly real *empty* wallet: no cash, no positions, no
  // trades. Equity, Today's P&L, the open list and the win rate all silently
  // dropped that engine while the baseline below still counted its capital, so
  // a single dropped connection (app.py restarting, or the single-threaded
  // HTTPServer closing on us mid-tick) produced a plausible-looking dashboard
  // that was wrong until the next tick. A failure is now carried as a marker
  // the renderer must handle, never as data.
  async function loadWallet(url) {
    try {
      const r = await fetch(url);
      if (!r.ok) return { __unavailable: `HTTP ${r.status}` };
      const j = await r.json();
      if (!j || typeof j !== 'object' || Array.isArray(j)) return { __unavailable: 'bad payload' };
      return j;
    } catch (e) {
      // Connection refused / dropped / body not JSON (an error page).
      return { __unavailable: 'no response' };
    }
  }

  async function refresh() {
    // loadWallet() never rejects, so this cannot throw.
    const [sig, pat, crs, brk] = await Promise.all([
      loadWallet('/live-portfolio'),
      loadWallet('/live-patterns-portfolio'),
      loadWallet('/live-cross-portfolio'),
      loadWallet('/live-breakout-portfolio'),
    ]);
    // Kept for the detail window and the stock page rail, which read the full
    // wallet (quotes, closed trades, ticker paths) rather than the summaries.
    lastWallets = { signals: sig, patterns: pat, cross: crs, breakout: brk };
    // Each consumer fails on its own: a bug in one must not blank the others.
    // Getting into a catch means a bug in this file, so it says so on screen
    // instead of leaving the last (now stale) numbers standing as if current.
    try {
      render(lastWallets);
    } catch (e) {
      const body = document.getElementById('live-widget-body');
      if (body) body.innerHTML = `<div class="lw-empty">Live widget error — see console</div>`;
      console.warn('[live_widget] render failed:', e);
    }
    try {
      renderRail();
    } catch (e) {
      const body = document.querySelector('#live-trades .lt-body');
      if (body) body.innerHTML = `<div class="lt-empty">Live trades error — see console</div>`;
      console.warn('[live_widget] stock page rail failed:', e);
    }
    // An open detail window follows the same 60 s poll, so its "now" price
    // and P&L never sit behind the row it was opened from.
    if (detail) {
      try { renderDetail(false); } catch (e) { console.warn('[live_widget] detail window failed:', e); }
    }
  }

  // Engine identity: shared across summarise() and the open-positions render
  // so colours and dot labels stay in lockstep.
  const ENGINES = {
    signals:  { label: 'Signals',  link: '/live-signals',  color: '#0052cc' },
    patterns: { label: 'Patterns', link: '/live-patterns', color: '#1a8042' },
    cross:    { label: 'Cross',    link: '/live-cross',    color: '#b08000' },
    breakout: { label: 'Breakout', link: '/live-breakout', color: '#7a4cc2' },
  };

  function summarise(p) {
    const cash = p.cash || 0;
    const positions = p.positions || {};
    const closed = p.closed_trades || [];
    const scored = closed.filter(t => t.reason !== 'STALE');
    const quotes = (p.last_quotes && typeof p.last_quotes === 'object') ? p.last_quotes : {};
    let openVal = 0;
    for (const [tk, pos] of Object.entries(positions)) {
      const last = (quotes[tk] && quotes[tk].last) || pos.entry_price;
      openVal += pos.shares * last;
    }
    return {
      cash,
      openVal,
      equity: cash + openVal,
      openCount: Object.keys(positions).length,
      // STALE = the engine was down past this position's exit date, so its exit
      // price is an artefact. Cash moved for real (stays in realised), but it
      // must not score. Duplicated from live_common.js on purpose: this widget
      // also loads on pages that do not include live_common.js.
      realized: closed.reduce((s, t) => s + (t.pnl_sek || 0), 0),
      total: scored.length,
      wins: scored.filter(t => (t.pnl_sek || 0) > 0).length,
      closed,
      positions,
      quotes,
      ticker_paths: (p.ticker_paths && typeof p.ticker_paths === 'object') ? p.ticker_paths : {},
    };
  }

  // Collect every open position across the three engines with computed P&L,
  // sorted by SEK P&L descending (winners on top). Each entry carries enough
  // context to render a row + a stock-detail link.
  function collectOpenPositions(engineSummaries) {
    const rows = [];
    for (const [key, eng] of Object.entries(engineSummaries)) {
      const meta = ENGINES[key];
      for (const [tk, pos] of Object.entries(eng.positions)) {
        const q    = eng.quotes[tk];
        const last = (q && q.last) || pos.entry_price;
        const ret  = last / pos.entry_price - 1;
        const pnl  = pos.shares * (last - pos.entry_price);
        const path = eng.ticker_paths[tk] || null;
        rows.push({
          ticker: tk,
          engineKey: key,
          engine: meta.label,
          color:  meta.color,
          engineLink: meta.link,
          stockLink:  path ? ('/view?path=' + encodeURIComponent(path)) : meta.link,
          entry: pos.entry_price,
          last,
          shares: pos.shares,
          entryTs: pos.entry_ts || pos.entry_date || '',
          ret,
          pnl,
          // source 'stale' = the live fetch failed on the last engine run and
          // the engine kept the previous good price, so this P&L is behind.
          // live_common.js flags it on every live page; the widget is the
          // summary shown on every OTHER page, so it must not be the one
          // place staleness is silent.
          stale: !!(q && q.source === 'stale'),
        });
      }
    }
    rows.sort((a, b) => b.pnl - a.pnl);
    return rows;
  }

  // ── recent trades ──────────────────────────────────────────────────────────
  // The widget's compact counterpart to the Live Overview's trade feed. It
  // shows the CLOSED side only: the block above already lists every open
  // position with a live return, so repeating them here would spend a third of
  // a 220px sidebar re-stating what is two rows up.
  //
  // 220px is not enough for the overview's ten columns, so each trade is two
  // dense lines — ticker + return + exit reason, then the two fill prices and
  // the exit clock — with the full detail (both timestamps, share count, P&L,
  // engine) in the row's tooltip.
  const FEED_ROWS = 6;

  // "2026-09-04T15:32:29" -> "09-04 15:32". Sliced rather than Date-parsed: the
  // engines write local wall-clock stamps with no zone, so parsing then
  // reformatting can shift the displayed hour.
  const tsShort = ts => ts ? String(ts).slice(5, 16).replace('T', ' ') : '';
  const tsLong  = ts => ts ? String(ts).replace('T', ' ').slice(0, 16) : '';

  // Short codes, because "TIMEOUT" alone is a third of the row width.
  const REASON_SHORT = { TIMEOUT: 'TIME', MANUAL: 'MAN' };

  // Identifies the record behind a row, so a click can open its detail window.
  // The entry stamp is part of it: a ticker can be bought again, and a window
  // opened on the old position must not quietly switch to the new one.
  const rowAttrs = (kind, r) =>
    `data-lw-kind="${kind}" data-lw-engine="${esc(r.engineKey)}" data-lw-ticker="${esc(r.ticker)}"`
    + ` data-lw-entry="${esc(r.entryTs)}"` + (kind === 'closed' ? ` data-lw-exit="${esc(r.exitTs)}"` : '');

  const CLICK_HINT = '\nClick for the entry, the planned exit and why it was bought';

  function collectClosedTrades(engineSummaries) {
    const rows = [];
    for (const [key, eng] of Object.entries(engineSummaries)) {
      const meta = ENGINES[key];
      for (const t of eng.closed) {
        const path = eng.ticker_paths[t.ticker] || null;
        rows.push({
          ticker: t.ticker,
          engineKey: key,
          engine: meta.label,
          color:  meta.color,
          stockLink: path ? ('/view?path=' + encodeURIComponent(path)) : meta.link,
          entry: t.entry_price,
          exit:  t.exit_price,
          shares: t.shares,
          entryTs: t.entry_ts || t.entry_date || '',
          exitTs:  t.exit_ts  || t.exit_date  || '',
          ret: t.ret_pct,
          pnl: t.pnl_sek,
          reason: t.reason || '',
        });
      }
    }
    // Newest exit first. ISO stamps sort lexicographically, so no parsing.
    rows.sort((a, b) => String(b.exitTs).localeCompare(String(a.exitTs)));
    return rows;
  }

  // Takes the already-built summaries (only the wallets that loaded), so an
  // unavailable engine contributes no rows rather than an empty-looking one.
  function renderTradeRows(summaries) {
    const all = collectClosedTrades(summaries);
    if (!all.length) return '<div class="lw-empty">No closed trades yet</div>';

    const shown = all.slice(0, FEED_ROWS);
    const rows = shown.map(r => {
      const tag   = REASON_SHORT[r.reason] || r.reason;
      const title = `${r.engine} · ${fmt(r.shares, 0)} sh · ${r.reason}\n`
                  + `Bought ${tsLong(r.entryTs)} @ ${fmt(r.entry, 2)}\n`
                  + `Sold ${tsLong(r.exitTs)} @ ${fmt(r.exit, 2)}\n`
                  + `${sign(r.pnl)} SEK` + CLICK_HINT;
      return `
        <a class="lw-trade" href="${r.stockLink}" title="${esc(title)}" ${rowAttrs('closed', r)}>
          <span class="lw-trade-top">
            <span class="lw-dot" style="background:${r.color};"></span>
            <span class="lw-trade-tk">${esc(r.ticker)}</span>
            <span class="lw-trade-ret ${cls(r.ret)}">${r.ret >= 0 ? '+' : ''}${(r.ret * 100).toFixed(2)}%</span>
            <span class="lw-trade-tag reason-${esc(r.reason)}">${esc(tag)}</span>
          </span>
          <span class="lw-trade-bot">
            <span class="lw-trade-px">${fmt(r.entry, 2)} → ${fmt(r.exit, 2)}</span>
            <span class="lw-trade-when">${tsShort(r.exitTs)}</span>
          </span>
        </a>`;
    }).join('');

    const more = all.length > shown.length
      ? `<a class="lw-more" href="/live-overview#trade-feed">+${all.length - shown.length} more in the full feed →</a>`
      : '';
    return `<div class="lw-trades">${rows}</div>${more}`;
  }

  function todayPnL(closedLists) {
    // Use the user's local date (matches the engine's date semantics)
    const today = new Date();
    const pad = n => String(n).padStart(2, '0');
    const todayStr = `${today.getFullYear()}-${pad(today.getMonth() + 1)}-${pad(today.getDate())}`;
    let pnl = 0, count = 0;
    for (const lst of closedLists) {
      for (const t of (lst || [])) {
        if (t.exit_date === todayStr) {
          pnl += (t.pnl_sek || 0);
          count += 1;
        }
      }
    }
    return { pnl, count };
  }

  // Render the open-position detail rows. A click opens the detail window;
  // the href (the stock's page if we have a ticker_path, else the engine's
  // tab) still serves modified clicks. Sorted winners-first via
  // collectOpenPositions.
  function renderPositionRows(summaries) {
    const rows = collectOpenPositions(summaries);
    if (!rows.length) return '';
    return `<div class="lw-positions">` + rows.map(r => {
      const retPct = (r.ret * 100).toFixed(2);
      const retStr = `${r.ret >= 0 ? '+' : ''}${retPct}%`;
      const title  = `${r.engine} · ${fmt(r.shares, 0)} sh @ ${fmt(r.entry, 2)} → ${fmt(r.last, 2)}  ·  ${sign(r.pnl)} SEK`
                   + (r.entryTs ? `\nBought ${tsLong(r.entryTs)}` : '')
                   + (r.stale ? `\nStale quote — previous good price, P&L may be behind` : '')
                   + CLICK_HINT;
      // Same marker + wording as live_common.js staleMark(), and the same
      // .q-stale token styling, so a stale row reads identically here and on
      // the live pages.
      const staleTag = r.stale
        ? ` <span class="q-stale" title="Live quote unavailable on the last run — this is the previous good price, so P&amp;L may be behind">stale</span>`
        : '';
      const tkDisp = r.ticker.length > 18 ? r.ticker.slice(0, 17) + '…' : r.ticker;
      return `
        <a class="lw-pos" href="${r.stockLink}" title="${esc(title)}" ${rowAttrs('open', r)}>
          <span class="lw-dot" style="background:${r.color};"></span>
          <span class="lw-pos-tk">${esc(tkDisp)}</span>${staleTag}
          <span class="lw-pos-ret ${cls(r.ret)}">${retStr}</span>
        </a>`;
    }).join('') + `</div>`;
  }

  // Warning strip for engines whose wallet did not load. Styled inline because
  // this widget also renders on pages that predate any .lw-warn rule in
  // style.css — a banner that says nothing because its CSS is missing would be
  // the same silent failure all over again.
  const WARN_STYLE = 'font-size:10.5px;line-height:1.35;color:var(--warning);'
                   + 'background:var(--warning-soft);border-radius:6px;'
                   + 'padding:5px 7px;margin-bottom:10px;';

  function render(d) {
    // Split the wallets into the ones that loaded and the ones that did not.
    // Everything below is computed over `summaries` alone, so a missing engine
    // is absent from BOTH the totals and the baseline — never counted on one
    // side and dropped on the other.
    const summaries = {};
    const missing = [];
    for (const [key, meta] of Object.entries(ENGINES)) {
      const w = d[key];
      if (!w || w.__unavailable) {
        missing.push({ key, label: meta.label, reason: (w && w.__unavailable) || 'no data' });
      } else {
        summaries[key] = summarise(w);
      }
    }
    const live = Object.keys(summaries);

    const body = document.getElementById('live-widget-body');
    if (!body) return;

    const warnTitle = missing.map(m => m.label + ': ' + m.reason).join('\n').replace(/"/g, '&quot;');
    const warn = missing.length
      ? `<div style="${WARN_STYLE}" title="${warnTitle}">`
        + (live.length
            ? `⚠ ${missing.map(m => m.label).join(', ')} unavailable — figures below exclude ${missing.length > 1 ? 'them' : 'it'}`
            : `⚠ No wallet answered — the app may be restarting`)
        + `</div>`
      : '';

    if (!live.length) {
      // Nothing loaded: show the warning and stop. A zeroed dashboard here
      // would read as "all three wallets are empty", which is a very different
      // claim from "the server did not answer".
      body.innerHTML = warn;
      return;
    }

    const s = summaries.signals, p = summaries.patterns, x = summaries.cross, b = summaries.breakout;
    const all = live.map(k => summaries[k]);
    const combinedEquity = all.reduce((a, e) => a + e.equity, 0);
    const combinedOpen = all.reduce((a, e) => a + e.openCount, 0);
    const combinedTrades = all.reduce((a, e) => a + e.total, 0);
    const combinedWins = all.reduce((a, e) => a + e.wins, 0);
    const wr = combinedTrades ? combinedWins / combinedTrades : null;
    const td = todayPnL(all.map(e => e.closed));
    // Contributed capital across the wallets that loaded — read from each state
    // file so a deposit moves the baseline rather than showing up as a gain.
    // No default: the old `|| 500000` fallback pre-dated the top-ups (all three
    // wallets sit at 1,000,000 now), so on a missing figure it halved the
    // baseline and doubled the apparent return. An unknown baseline now shows
    // as unknown.
    let startCapital = 0, baselineKnown = true;
    for (const key of live) {
      const ws = Number(d[key].wallet_start);
      if (!isFinite(ws) || ws <= 0) { baselineKnown = false; break; }
      startCapital += ws;
    }
    const equityPct = baselineKnown ? (combinedEquity / startCapital - 1) * 100 : null;
    const openTxt = key => summaries[key] ? summaries[key].openCount : '—';

    body.innerHTML = warn + `
      <div class="lw-block">
        <div class="lw-lbl">Equity${missing.length ? ` · ${live.length}/${Object.keys(ENGINES).length} wallets` : ''}</div>
        <div class="lw-val">${fmt(combinedEquity, 0)} <span class="lw-unit">SEK</span></div>
        ${equityPct === null
          ? `<div class="lw-sub">baseline unavailable</div>`
          : `<div class="lw-sub ${cls(equityPct)}">${equityPct >= 0 ? '+' : ''}${equityPct.toFixed(2)}% vs ${(startCapital/1000).toFixed(0)}k</div>`}
      </div>
      <div class="lw-block">
        <div class="lw-lbl">Today's P&amp;L</div>
        <div class="lw-val ${cls(td.pnl)}">${sign(td.pnl)} <span class="lw-unit">SEK</span></div>
        <div class="lw-sub">${td.count} closed today</div>
      </div>
      <div class="lw-block">
        <div class="lw-lbl">Open positions · ${combinedOpen}</div>
        ${renderPositionRows(summaries) || `<div class="lw-empty">No open positions</div>`}
        <div class="lw-engine-counts" title="By engine">
          <a class="lw-mini" href="/live-signals" title="Signals — ${s ? s.openCount + ' open' : 'unavailable'}">
            <span class="lw-dot" style="background:${ENGINES.signals.color};"></span>${openTxt('signals')}
          </a>
          <a class="lw-mini" href="/live-patterns" title="Patterns — ${p ? p.openCount + ' open' : 'unavailable'}">
            <span class="lw-dot" style="background:${ENGINES.patterns.color};"></span>${openTxt('patterns')}
          </a>
          <a class="lw-mini" href="/live-cross" title="Cross — ${x ? x.openCount + ' open' : 'unavailable'}">
            <span class="lw-dot" style="background:${ENGINES.cross.color};"></span>${openTxt('cross')}
          </a>
          <a class="lw-mini" href="/live-breakout" title="Breakout — ${b ? b.openCount + ' open' : 'unavailable'}">
            <span class="lw-dot" style="background:${ENGINES.breakout.color};"></span>${openTxt('breakout')}
          </a>
        </div>
      </div>
      <div class="lw-block">
        <div class="lw-lbl">Win rate</div>
        <div class="lw-val">${wr === null ? '—' : (wr * 100).toFixed(0) + '%'}</div>
        <div class="lw-sub">${combinedWins}W / ${combinedTrades - combinedWins}L · ${combinedTrades} total</div>
      </div>
      <div class="lw-block">
        <div class="lw-lbl">Recent trades</div>
        ${renderTradeRows(summaries)}
      </div>
    `;
  }

  // ── stock detail window ────────────────────────────────────────────────────
  // What a click on any stock row opens. Everything in it comes from the
  // wallet JSON this widget already polls: an open position stores its exact
  // fill, `tp`, `sl` and `target_exit_date`, and every position stores
  // `signal_info` — the figures its engine measured at the moment it bought
  // (written by live_*.py at entry, copied onto the closed trade by
  // apply_exit_checks). A closed trade does NOT keep tp/sl, so its plan is
  // rebuilt from the strategy version that opened it, with the same
  // entry × (1 ± pct) the engines use, and the window says so.
  //
  // The ledger (strategy rules), tier map and sector map are fetched once, on
  // the first open, so a page that never opens the window pays nothing.

  let lastWallets = null;   // raw wallet JSON by engine key, as of the last poll
  let detail      = null;   // {kind, engine, ticker, entryTs, exitTs} while open
  let detailFrom  = null;   // the row that opened it — focus goes back there
  let detailScope = null;   // the list it sits in: the widget or the stock page rail
  let modal       = null;
  const ref = { state: 'idle', ledger: null, tiers: {}, sectors: {} };

  // live_signals.CRASH_GUARD — a Signals buy needs the 20-day return above
  // this. Unchanged since the engine's first commit, and not in the ledger's
  // params text, so it is restated here.
  const CRASH_GUARD = -0.15;

  // One line per detector in live_patterns.py, worded from its actual test.
  // All but Three White Soldiers also run _downtrend_before(): the close before
  // the pattern must sit 1.5%+ under the close about five sessions earlier.
  const PATTERN_INFO = {
    'Bullish Engulfing':    'a red candle, then a green one that opens at or below its close and closes at or above its open, after a 1.5%+ slide',
    'Tweezer Bottom':       'two candles with the same low (within 0.3%), the second one green, after a 1.5%+ slide',
    'Morning Star':         'a long red candle, a small-bodied one, then a green candle closing above the first one\'s midpoint, after a 1.5%+ slide',
    'Piercing Line':        'a long red candle, then a green one that opens below its close and closes above its midpoint, after a 1.5%+ slide',
    'Three White Soldiers': 'three long green candles in a row, each opening and closing higher, with short upper wicks',
    'Hammer':               'a green candle with a small body and a lower wick at least twice the body, after a 1.5%+ slide (detect-only since 2026-08-18)',
  };

  const LEAD = {
    signals:  'Mean reversion — buys a stock that closed sharply lower on the day, for a rebound within the hold.',
    patterns: 'Candlestick reversal — buys after bullish patterns on the last completed daily candle.',
    cross:    'Confluence — the Signals day-drop and same-day bullish candlestick patterns, on the same stock.',
    breakout: 'Momentum — buys a fresh 52-week closing high among the 150 most-traded SEK stocks, for up to 12 weeks.',
  };

  // Plain-English exit reasons, matching the branches in
  // live_signals.apply_exit_checks (shared by all four engines).
  const EXIT_TEXT = {
    TP:      'take-profit hit — booked at the target (limit sell)',
    SL:      'stop-loss hit — booked at the stop, or at the market if the price was already below it',
    TIMEOUT: 'hold period ran out — sold at the market on the exit-by day',
    MANUAL:  'sold by you (Sell now) at the market price',
    STALE:   'the engine was down past the exit date — booked at the price when it came back, and left out of the win rate',
  };

  // Prices exactly as the engine stored them. TP/SL are entry × (1 ± pct)
  // rounded to 4 decimals (225,364 say), so a 2-decimal format would misquote
  // the very levels this window exists to show.
  const fmtPx  = n => (n == null || !isFinite(n)) ? '—'
    : Number(n).toLocaleString('sv-SE', { minimumFractionDigits: 2, maximumFractionDigits: 4 });
  const pctTxt = (x, d = 2) => (x == null || !isFinite(x)) ? '—' : `${x >= 0 ? '+' : ''}${(x * 100).toFixed(d)}%`;
  const num    = v => (v == null || v === '' || !isFinite(Number(v))) ? null : Number(v);
  const round4 = x => Math.round(x * 1e4) / 1e4;
  const tsFull = ts => ts ? String(ts).replace('T', ' ').slice(0, 19) : '';
  const plural = (n, w) => `${n} ${w}${n === 1 ? '' : 's'}`;
  const dayNum = iso => {
    const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(iso || ''));
    return m ? Date.UTC(+m[1], +m[2] - 1, +m[3]) / 864e5 : null;
  };
  const daysBetween = (a, b) => {
    const x = dayNum(a), y = dayNum(b);
    return (x == null || y == null) ? null : Math.round(y - x);
  };
  const WEEKDAY = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
  const weekday = iso => { const n = dayNum(iso); return n == null ? '' : WEEKDAY[new Date(n * 864e5).getUTCDay()]; };
  const pad2 = n => String(n).padStart(2, '0');
  const localIso = () => { const t = new Date(); return `${t.getFullYear()}-${pad2(t.getMonth() + 1)}-${pad2(t.getDate())}`; };
  const clock = () => { const t = new Date(); return `${pad2(t.getHours())}:${pad2(t.getMinutes())}`; };

  // Bound to the widget and, on a stock page, to the Live trades rail.
  function onRowClick(e) {
    const a = e.target.closest('[data-lw-kind]');
    if (!a || !e.currentTarget.contains(a)) return;
    // Modified clicks keep the link's own behaviour (stock or engine page, new tab).
    if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    e.preventDefault();
    openDetail({
      kind:    a.dataset.lwKind,
      engine:  a.dataset.lwEngine,
      ticker:  a.dataset.lwTicker,
      entryTs: a.dataset.lwEntry || '',
      exitTs:  a.dataset.lwExit || '',
    }, a, e.currentTarget);
  }

  function ensureModal() {
    if (modal) return modal;
    modal = $('div', { class: 'lwm-backdrop' });
    modal.innerHTML = `
      <div class="lwm-dialog" role="dialog" aria-modal="true" aria-labelledby="lwm-title" tabindex="-1">
        <button type="button" class="lwm-close" aria-label="Close" title="Close (Esc)">×</button>
        <div class="lwm-content"></div>
      </div>`;
    // mousedown, not click: a text selection dragged out of the dialog ends
    // with a click on the backdrop, and that must not close the window.
    modal.addEventListener('mousedown', e => { if (e.target === modal) closeDetail(); });
    modal.querySelector('.lwm-close').addEventListener('click', closeDetail);
    document.addEventListener('keydown', onModalKey);
    document.body.appendChild(modal);
    return modal;
  }

  function onModalKey(e) {
    if (!detail) return;
    if (e.key === 'Escape') { e.preventDefault(); closeDetail(); return; }
    if (e.key !== 'Tab') return;
    // Keep Tab inside the window while it is open.
    const items = [...modal.querySelectorAll('a[href], button:not([disabled])')]
      .filter(el => el.offsetParent !== null);
    if (!items.length) return;
    const first = items[0], last = items[items.length - 1];
    const inside = modal.contains(document.activeElement);
    if (e.shiftKey && (!inside || document.activeElement === first || document.activeElement === modal.querySelector('.lwm-dialog'))) {
      e.preventDefault(); last.focus();
    } else if (!e.shiftKey && (!inside || document.activeElement === last)) {
      e.preventDefault(); first.focus();
    }
  }

  function openDetail(spec, from, scope) {
    ensureModal();
    detail = spec;
    detailFrom = from || null;
    detailScope = scope || root;
    modal.querySelector('.lwm-content').innerHTML = '';
    renderDetail(true);
    modal.classList.add('open');
    modal.querySelector('.lwm-dialog').focus();
    loadRef();
  }

  function closeDetail() {
    if (!modal || !detail) return;
    const spec = detail;
    detail = null;
    modal.classList.remove('open');
    // The widget and the rail re-render every 60 s, so the row that opened the
    // window may have been replaced by an identical one; find that one instead.
    // The entry stamp is in the selector because the rail can list several
    // trades of one stock in one engine.
    let back = detailFrom && detailFrom.isConnected ? detailFrom : null;
    if (!back && detailScope && detailScope.isConnected) {
      const q = v => (window.CSS && CSS.escape) ? CSS.escape(v) : String(v).replace(/["\\]/g, '\\$&');
      back = detailScope.querySelector(`[data-lw-kind="${q(spec.kind)}"][data-lw-engine="${q(spec.engine)}"]`
                                     + `[data-lw-ticker="${q(spec.ticker)}"]`
                                     + (spec.entryTs ? `[data-lw-entry="${q(spec.entryTs)}"]` : ''));
    }
    if (back) back.focus({ preventScroll: true });
    detailFrom = null;
    detailScope = null;
  }

  // Strategy rules, tiers and sectors — one fetch each, on the first open. A
  // failed ledger read is retried on the next open instead of leaving every
  // later window without its rules until the page reloads.
  function loadRef() {
    if (ref.state === 'loading' || ref.state === 'ok') return;
    ref.state = 'loading';
    const get = url => fetch(url).then(r => (r.ok ? r.json() : null)).catch(() => null);
    const isMap = m => m && typeof m === 'object' && !Array.isArray(m);
    Promise.all([get('/changelog-data'), get('/live-tiers'), get('/live-sectors')])
      .then(([ledger, tiers, sectors]) => {
        if (isMap(tiers))   ref.tiers = tiers;
        if (isMap(sectors)) ref.sectors = sectors;
        ref.ledger = (isMap(ledger) && !ledger.error && isMap(ledger.strategy_history)) ? ledger : null;
        ref.state = ref.ledger ? 'ok' : 'failed';
        if (detail) renderDetail(false);
      });
  }

  const verRows = engine => {
    const h = ref.ledger && ref.ledger.strategy_history;
    return (h && Array.isArray(h[engine])) ? h[engine] : [];
  };
  const verRow = (engine, ver) => verRows(engine).find(r => r.version === ver) || null;

  // The TP/SL pair a strategy version traded, parsed from its ledger `params`
  // text and carried forward from the latest earlier version when a row does
  // not restate it (x-2: "entries paused, exits still managed"). The same rule
  // as verBreakeven() in live_common.js, which these pages do not load.
  function verPair(engine, ver) {
    const chron = [...verRows(engine)].sort((a, b) =>
      String(a.from || '9999').localeCompare(String(b.from || '9999')));
    for (let i = chron.findIndex(r => r.version === ver); i >= 0; i--) {
      const m = /TP\s*\+?(\d+(?:\.\d+)?)\s*%\s*\/\s*SL\s*-?(\d+(?:\.\d+)?)\s*%/i.exec(chron[i].params || '');
      if (m) return { tp: parseFloat(m[1]) / 100, sl: -Math.abs(parseFloat(m[2])) / 100 };
    }
    return null;
  }

  // Signals' day-drop trigger as that version states it ("DoD <= -2%").
  // Versions before sig-10 do not state one, and then no threshold is shown.
  function verDodRule(engine, ver) {
    const row = verRow(engine, ver);
    const m = row && /DoD\s*<=\s*(-?\d+(?:\.\d+)?)\s*%/i.exec(row.params || '');
    return m ? parseFloat(m[1]) / 100 : null;
  }

  // Open: the position as stored. If it has closed while the window was open,
  // the closed trade with the same entry stamp, flagged as such.
  function findRecord(spec) {
    const w = lastWallets && lastWallets[spec.engine];
    if (!w || w.__unavailable) return null;
    const positions = (w.positions && typeof w.positions === 'object') ? w.positions : {};
    const closed = Array.isArray(w.closed_trades) ? w.closed_trades : [];
    const sameEntry = r => !spec.entryTs || (r.entry_ts || r.entry_date || '') === spec.entryTs;
    const closedMatch = t => t.ticker === spec.ticker && sameEntry(t)
      && (spec.kind !== 'closed' || !spec.exitTs || (t.exit_ts || t.exit_date || '') === spec.exitTs);
    if (spec.kind === 'open') {
      const pos = positions[spec.ticker];
      if (pos && sameEntry(pos)) return { kind: 'open', rec: pos, wallet: w };
      const t = closed.find(closedMatch);
      return t ? { kind: 'closed', rec: t, wallet: w, closedSince: true } : null;
    }
    const t = closed.find(closedMatch);
    return t ? { kind: 'closed', rec: t, wallet: w } : null;
  }

  function renderDetail(resetScroll) {
    if (!modal || !detail) return;
    const dlg  = modal.querySelector('.lwm-dialog');
    const body = modal.querySelector('.lwm-content');
    const f = findRecord(detail);
    if (!f) {
      // Keep the last figures on screen, but say they stopped updating — a
      // window that silently freezes is the failure this project keeps hitting.
      const w = lastWallets && lastWallets[detail.engine];
      const label = (ENGINES[detail.engine] || {}).label || detail.engine;
      const msg = (!w || w.__unavailable)
        ? `The ${label} wallet did not answer on the last refresh (${clock()}) — these are the previous figures.`
        : `No longer in the ${label} wallet as of ${clock()} — these are the last figures loaded.`;
      let flag = body.querySelector('.lwm-flag.gone');
      if (!flag) {
        flag = $('div', { class: 'lwm-flag gone' });
        body.insertBefore(flag, body.firstChild);
      }
      flag.textContent = msg;
      return;
    }
    const top = dlg.scrollTop;
    const hadFocus = body.contains(document.activeElement);
    body.innerHTML = detailHtml(detail, f);
    dlg.scrollTop = resetScroll ? 0 : top;
    if (hadFocus) dlg.focus({ preventScroll: true });
  }

  function verPillHtml(engine, rec) {
    const ver = rec.strategy_version;
    if (!ver) return '';
    const row = verRow(engine, ver);
    const current = !!(ref.ledger && ref.ledger.strategy_versions
                       && ref.ledger.strategy_versions[engine] === ver);
    const back = rec.version_source === 'backfilled';
    const tip = [
      row && row.from ? `${ver} live from ${row.from}` : ver,
      rec.app_version ? `opened by app v${rec.app_version}` : '',
      back ? 'version inferred from the entry date' : 'version stamped by the engine at entry',
      current ? 'still the current version' : '',
    ].filter(Boolean).join('\n');
    return `<a class="pill ver-pill${back ? ' backfilled' : ''}${current ? ' current' : ''}" `
         + `href="/changelog#${encodeURIComponent(ver)}" title="${esc(tip)}">${esc(ver)}${back ? '*' : ''}</a>`;
  }

  // A horizontal map of the bet: stop on the left, target on the right, entry
  // where it falls between them, and today's price (or the exit) as a dot.
  function ladderHtml(entry, tp, sl, mark, markLbl) {
    if (![entry, tp, sl].every(v => v != null && isFinite(v)) || !(tp > sl)) return '';
    const lo = Math.min(sl, entry), hi = Math.max(tp, entry), span = hi - lo;
    if (!(span > 0)) return '';
    const at = v => Math.max(0, Math.min(100, (v - lo) / span * 100));
    const e = at(entry).toFixed(2);
    let dot = '';
    if (mark != null && isFinite(mark)) {
      const x = at(mark), ret = mark / entry - 1;
      const align = x < 14 ? ' al-l' : (x > 86 ? ' al-r' : '');
      dot = `<span class="lwm-lad-mark ${cls(ret)}" style="left:${x.toFixed(2)}%">`
          + `<span class="lwm-lad-mlbl${align}">${markLbl} ${fmtPx(mark)} `
          + `<span class="${cls(ret)}">${pctTxt(ret)}</span></span></span>`;
    }
    return `
      <div class="lwm-ladder" aria-hidden="true">
        <div class="lwm-lad-track">
          <span class="lwm-lad-loss" style="width:${e}%"></span>
          <span class="lwm-lad-entry" style="left:${e}%"></span>
          ${dot}
        </div>
        <div class="lwm-lad-labels">
          <span class="lwm-lad-sl">Stop ${pctTxt(sl / entry - 1, 1)}</span>
          <span class="lwm-lad-en" style="left:${e}%">Entry</span>
          <span class="lwm-lad-tp">Target ${pctTxt(tp / entry - 1, 1)}</span>
        </div>
      </div>`;
  }

  function fillText(engine, rec) {
    if (rec.fill_type === 'close') return 'filled at the official close';
    if (rec.fill_type === 'auto')  return 'filled intraday at the ask';
    if (rec.fill_type === 'limit') return 'filled by your limit order';
    return engine === 'patterns' ? 'filled at the ask during the session' : '';
  }

  // ── why it was bought: one builder per engine, all reading signal_info ────
  // Each check is {ok, text, note, list}: ok=true passed, false failed (a
  // forced entry can carry one), null = context rather than a gate.

  const patternsOf = si =>
    (Array.isArray(si.all_patterns) && si.all_patterns.length) ? si.all_patterns
      : (si.pattern ? [si.pattern] : []);
  const patternItems = pats => pats.map(n => ({ b: n, t: PATTERN_INFO[n] || 'no description on file' }));

  function whySignals(si, rec) {
    const checks = [];
    const rule = verDodRule('signals', rec.strategy_version);
    if (si.dod != null) {
      checks.push({
        ok: rule == null ? null : si.dod <= rule + 1e-9,
        text: `Fell ${pctTxt(si.dod)} on the day`,
        note: [
          (si.yest != null && si.today != null) ? `previous close ${fmtPx(si.yest)} → close ${fmtPx(si.today)}` : '',
          rule != null ? `rule: a drop of ${+(Math.abs(rule) * 100).toFixed(2)}% or more` : '',
        ].filter(Boolean).join(' · '),
      });
    }
    if (si.ret_20d != null) checks.push({
      ok: si.ret_20d > CRASH_GUARD,
      text: `20-day return ${pctTxt(si.ret_20d, 1)}`,
      note: `crash guard — names down more than ${Math.round(-CRASH_GUARD * 100)}% over 20 sessions are skipped`,
    });
    if (si.is_friday != null) checks.push({
      ok: !si.is_friday, text: si.is_friday ? 'Bought on a Friday' : 'Not a Friday',
      note: 'the engine does not buy on Fridays',
    });
    if (si.at_5d_high != null) checks.push({
      ok: !si.at_5d_high, text: si.at_5d_high ? 'At its 5-day high' : 'Not at its 5-day high',
    });
    if (si.forced) {
      checks.push({ ok: null, text: 'Forced by you', note: 'you overrode the filter on a near-miss' });
    } else if (si.from_watchlist) {
      checks.push({ ok: null, text: 'Approved by you from the watchlist', note: 'the filter still had to pass' });
    } else {
      checks.push({ ok: null, text: 'Automatic entry', note: 'the engine\'s own scan, no manual input' });
    }
    const n = si.news;
    if (n && typeof n === 'object') {
      if (n.error) {
        checks.push({ ok: null, text: 'News lookup failed at entry', note: String(n.error).slice(0, 160) });
      } else {
        const types = Object.entries(n.by_type || {}).map(([k, v]) => `${v} ${k}`).join(', ');
        checks.push({
          ok: null,
          text: n.n ? `${plural(n.n, 'news item')} before the buy` : 'No company news before the buy',
          note: [types, n.window ? String(n.window).replace('->', '→') : '',
                 'recorded only — news never gates an entry'].filter(Boolean).join(' · '),
          list: (Array.isArray(n.headlines) ? n.headlines : []).slice(0, 5).map(h => ({ t: h })),
        });
      }
    }
    return checks;
  }

  function whyPatterns(si) {
    const pats = patternsOf(si);
    if (!pats.length) return [];
    return [{
      ok: true,
      text: `${pats.join(' + ')} on the last completed daily candle`,
      note: pats.length > 1 ? `${pats.length} entry-eligible patterns on one candle (confluence)` : '',
      list: patternItems(pats),
    }];
  }

  function whyCross(si) {
    const checks = [];
    if (si.dod != null) {
      checks.push({ ok: true, text: `Signals half: fell ${pctTxt(si.dod)} on the day`,
                    note: si.ret_20d != null ? `20-day return ${pctTxt(si.ret_20d, 1)}` : '' });
    } else {
      checks.push({ ok: true, text: 'Signals half: on the Signals list that day',
                    note: 'this Cross generation recorded no day-drop figures' });
    }
    const pats = patternsOf(si);
    if (pats.length) checks.push({
      ok: true, text: `Patterns half: ${pats.join(' + ')}`,
      note: pats.length > 1 ? `${pats.length} patterns on the same day` : '',
      list: patternItems(pats),
    });
    return checks;
  }

  function whyBreakout(si, rec) {
    const checks = [];
    if (si.prior_high != null) checks.push({
      ok: true,
      text: `New 52-week closing high, ${pctTxt(si.breakout_pct, 2)} through the old one`,
      note: `closed ${fmtPx(rec.entry_price)} against a prior high of ${fmtPx(si.prior_high)}`,
    });
    checks.push({ ok: true, text: 'Fresh: no new 52-week high in the 20 sessions before',
                  note: 'a name that has just made one cannot trigger' });
    if (si.mom_12_1 != null) checks.push({
      ok: null, text: `12-1 momentum ${pctTxt(si.mom_12_1, 1)}`,
      note: 'return from 12 months to 1 month ago — sets the buying order when more names trigger than there are free slots',
    });
    if (si.universe_rank != null) checks.push({
      ok: null, text: `Turnover rank ${si.universe_rank} of 150`,
      note: si.turnover_msek != null
        ? `${fmt(si.turnover_msek, 1)} MSEK median daily turnover — the universe is the 150 most-traded`
        : 'the universe is the 150 most-traded',
    });
    return checks;
  }

  const WHY = { signals: whySignals, patterns: whyPatterns, cross: whyCross, breakout: whyBreakout };

  function checkHtml(c) {
    const kind = c.ok === true ? 'ok' : (c.ok === false ? 'no' : 'ctx');
    const icon = c.ok === true ? '✓' : (c.ok === false ? '✕' : '•');
    const list = (c.list && c.list.length)
      ? `<div class="lwm-sublist">${c.list.map(it =>
          `<div>${it.b ? `<b>${esc(it.b)}</b> — ` : ''}${esc(it.t)}</div>`).join('')}</div>`
      : '';
    return `<div class="lwm-check"><span class="lwm-ck ${kind}" aria-hidden="true">${icon}</span>`
         + `<div class="lwm-check-body"><div class="lwm-check-main">${esc(c.text)}</div>`
         + (c.note ? `<div class="lwm-check-note">${esc(c.note)}</div>` : '')
         + list + `</div></div>`;
  }

  // Same scoring as the live pages: realised P&L counts every exit, win rate
  // and average return count non-STALE exits only.
  function record(list) {
    const scored = list.filter(t => t.reason !== 'STALE');
    const wins = scored.filter(t => (t.pnl_sek || 0) > 0).length;
    return {
      n: scored.length, all: list.length, wins,
      wr:  scored.length ? wins / scored.length : null,
      avg: scored.length ? scored.reduce((s, t) => s + (t.ret_pct || 0), 0) / scored.length : null,
      pnl: list.reduce((s, t) => s + (t.pnl_sek || 0), 0),
    };
  }

  function recordRowHtml(label, r, be) {
    const k = `<span class="lwm-rec-k" title="${esc(label)}">${esc(label)}</span>`;
    if (!r.all) return k + `<span class="lwm-rec-none">no closed trades yet</span>`;
    const wrCls = (be == null || r.wr == null) ? '' : (r.wr > be ? 'up' : (r.wr < be ? 'down' : ''));
    const wrTip = be == null ? '' : `break-even ${Math.round(be * 100)}% for this version's TP/SL pair`;
    const nTip  = r.all > r.n ? `${r.all - r.n} STALE exit(s) left out of the win rate and average` : '';
    return k
      + `<span class="lwm-rec-n" title="${esc(nTip)}">${r.n}${r.all > r.n ? `<span class="lwm-rec-x">+${r.all - r.n}</span>` : ''}</span>`
      + `<span class="lwm-rec-wr ${wrCls}" title="${esc(wrTip)}">${r.wr == null ? '—' : Math.round(r.wr * 100) + '%'}</span>`
      // Coloured on the value as shown: an average of +0.000004 prints as
      // +0.00% and must not come out green.
      + `<span class="${r.avg == null || Math.abs(r.avg) < 5e-5 ? '' : cls(r.avg)}">${pctTxt(r.avg)}</span>`
      + `<span class="${cls(r.pnl)}">${sign(r.pnl)}</span>`;
  }

  function trackRecordHtml(spec, f, si) {
    const rec = f.rec;
    const closed = Array.isArray(f.wallet.closed_trades) ? f.wallet.closed_trades : [];
    // A closed trade is part of its version's record but is left out of
    // "other trades" in the same stock / pattern.
    const others = closed.filter(t => t !== rec);
    const ver = rec.strategy_version;
    const rows = [];
    let beNote = '';
    if (ver) {
      const pair = verPair(spec.engine, ver);
      const be = pair ? Math.abs(pair.sl) / (pair.tp + Math.abs(pair.sl)) : null;
      if (be != null) beNote = `${ver} wins at ${pctTxt(pair.tp, 1)} and loses at ${pctTxt(pair.sl, 1)}, so it breaks even at a ${Math.round(be * 100)}% win rate.`;
      rows.push(recordRowHtml(`This version · ${ver}`, record(closed.filter(t => t.strategy_version === ver)), be));
    }
    rows.push(recordRowHtml(`Other ${spec.ticker} trades`, record(others.filter(t => t.ticker === spec.ticker)), null));
    const pat = (spec.engine === 'patterns' || spec.engine === 'cross') ? si.pattern : null;
    if (pat) rows.push(recordRowHtml(`Other ${pat} trades`,
      record(others.filter(t => (t.signal_info || {}).pattern === pat)), null));
    const label = (ENGINES[spec.engine] || {}).label || spec.engine;
    return `
      <div class="lwm-sec">
        <div class="lwm-sec-title">Track record <span class="lwm-sec-note">— closed ${esc(label)} trades</span></div>
        <div class="lwm-rec">
          <div class="lwm-rec-row lwm-rec-head"><span></span><span>Trades</span><span>Win rate</span><span>Avg</span><span>P&amp;L SEK</span></div>
          ${rows.map(r => `<div class="lwm-rec-row">${r}</div>`).join('')}
        </div>
        <div class="lwm-foot-note">${beNote ? esc(beNote) + ' ' : ''}Under roughly 200 trades, a win rate is mostly noise.</div>
      </div>`;
  }

  function detailHtml(spec, f) {
    const meta   = ENGINES[spec.engine] || { label: spec.engine, link: '/live', color: '#8a94a6' };
    const w      = f.wallet;
    const rec    = f.rec;
    const isOpen = f.kind === 'open';
    const tk     = spec.ticker;
    const si     = (rec.signal_info && typeof rec.signal_info === 'object') ? rec.signal_info : {};
    const ver    = rec.strategy_version || null;
    const path   = (w.ticker_paths && w.ticker_paths[tk]) || null;
    // Opened from the rail on that stock's own page, a "Stock page" button
    // would only reload the page underneath.
    const stockBtn = !!path && !(location.pathname === '/view'
                                 && new URLSearchParams(location.search).get('path') === path);
    const entry  = num(rec.entry_price);
    const shares = num(rec.shares) || 0;
    const today  = localIso();
    const loadingRules = ref.state === 'idle' || ref.state === 'loading';

    // ── the plan: stored on an open position, rebuilt for a closed one ──────
    let tp = null, sl = null, planNote = '';
    if (isOpen) {
      tp = num(rec.tp); sl = num(rec.sl);
    } else {
      const pair = ver ? verPair(spec.engine, ver) : null;
      if (pair && entry != null) {
        tp = round4(entry * (1 + pair.tp));
        sl = round4(entry * (1 + pair.sl));
        planNote = `Target and stop rebuilt from ${ver}'s rules (TP ${pctTxt(pair.tp, 1)} / SL ${pctTxt(pair.sl, 1)}) — a closed trade does not keep its own.`;
      } else if (loadingRules && ver) {
        planNote = 'Loading the strategy rules…';
      } else {
        planNote = ver ? `No TP/SL on record for ${ver}, and a closed trade does not keep its own.`
                       : 'No strategy version on this trade, and a closed trade does not keep its own TP/SL.';
      }
    }

    // ── header ──────────────────────────────────────────────────────────────
    const tier   = ref.tiers[tk] || si.signals_tier || '';
    const sector = ref.sectors[tk] || '';
    const pills = [
      tier   ? `<span class="pill tier-pill">${esc(tier)}</span>` : '',
      sector ? `<span class="pill sector-pill" title="${esc(sector)}">${esc(sector)}</span>` : '',
      verPillHtml(spec.engine, rec),
      (!isOpen && rec.reason) ? `<span class="pill reason-${esc(rec.reason)}">${esc(rec.reason)}</span>` : '',
    ].join('');
    const flag = f.closedSince
      ? `<div class="lwm-flag">Closed since this window opened — ${esc(rec.reason || '')} at ${fmtPx(rec.exit_price)}, ${esc(tsFull(rec.exit_ts) || rec.exit_date || '')}.</div>`
      : '';

    // ── where it stands now / how it ended ──────────────────────────────────
    let statusLine, mark, markLbl;
    if (isOpen) {
      const q = (w.last_quotes && w.last_quotes[tk]) || null;
      const last = q ? num(q.last) : null;
      const held = daysBetween(rec.entry_date, today);
      const heldTxt = held == null ? '' : (held <= 0 ? 'bought today' : `held ${plural(held, 'day')}`);
      if (last != null && entry) {
        const ret = last / entry - 1, pnl = shares * (last - entry);
        const stale = q.source === 'stale'
          ? ' <span class="q-stale" title="Live quote unavailable on the last run — this is the previous good price, so P&amp;L may be behind">stale</span>'
          : '';
        statusLine = `Now <b>${fmtPx(last)}</b>${stale} <span class="${cls(ret)}">${pctTxt(ret)}</span>`
                   + ` · <span class="${cls(pnl)}">${sign(pnl)} SEK</span> unrealised`
                   + (heldTxt ? ` · ${heldTxt}` : '');
      } else {
        statusLine = 'No live quote yet' + (heldTxt ? ` · ${heldTxt}` : '');
      }
      mark = last; markLbl = 'Now';
    } else {
      const ret = num(rec.ret_pct), pnl = num(rec.pnl_sek);
      const held = daysBetween(rec.entry_date, rec.exit_date);
      statusLine = `Result <b class="${ret == null ? '' : cls(ret)}">${pctTxt(ret)}</b>`
                 + ` · <span class="${pnl == null ? '' : cls(pnl)}">${pnl == null ? '—' : sign(pnl)} SEK</span>`
                 + (held == null ? '' : ` · held ${held <= 0 ? 'under a day' : plural(held, 'day')}`);
      mark = num(rec.exit_price); markLbl = 'Exit';
    }

    // ── the four numbers: entry, target, stop, exit ─────────────────────────
    const tile = (lbl, val, sub, valCls = '') =>
      `<div class="lwm-tile"><div class="lwm-tile-lbl">${lbl}</div>`
      + `<div class="lwm-tile-val ${valCls}">${val}</div><div class="lwm-tile-sub">${sub}</div></div>`;
    // Two lines on purpose: "+3.00% · +2 099 SEK" does not fit a quarter of
    // the dialog and used to break between the amount and its unit.
    const levelSub = lvl => (lvl == null || !entry)
      ? (loadingRules && !isOpen ? 'loading…' : 'not recorded')
      : `${pctTxt(lvl / entry - 1)}<br>${sign(shares * (lvl - entry))} SEK`;
    const levelVal = lvl => lvl == null ? (loadingRules && !isOpen ? '…' : '—') : fmtPx(lvl);
    let lastTile;
    if (isOpen) {
      const left = daysBetween(today, rec.target_exit_date);
      const leftTxt = left == null ? '' : left > 1 ? `in ${left} days` : left === 1 ? 'tomorrow'
                    : left === 0 ? 'today' : `${plural(-left, 'day')} overdue`;
      lastTile = tile('Exit by', esc(rec.target_exit_date || '—'),
                      esc([weekday(rec.target_exit_date), leftTxt].filter(Boolean).join(' · ')));
    } else {
      const ret = num(rec.ret_pct);
      lastTile = tile('Exit', fmtPx(rec.exit_price),
                      esc(tsLong(rec.exit_ts) || rec.exit_date || ''), ret == null ? '' : cls(ret));
    }
    const tiles = tile('Entry', fmtPx(entry), esc(tsLong(rec.entry_ts) || rec.entry_date || ''))
                + tile(isOpen ? 'Take profit' : 'Planned TP', levelVal(tp), levelSub(tp), 'up')
                + tile(isOpen ? 'Stop loss' : 'Planned SL', levelVal(sl), levelSub(sl), 'down')
                + lastTile;

    // ── how each exit actually fills ────────────────────────────────────────
    const lines = [];
    if (isOpen) {
      if (spec.engine === 'breakout' && !rec.tp_armed && rec.tp_live_from) {
        lines.push(`<b>Target</b> switches on ${esc(rec.tp_live_from)} (session 20). If the price is already above ${fmtPx(tp)} then, it sells at the market; after that it is a limit sell at ${fmtPx(tp)}.`);
      } else {
        lines.push(`<b>Target</b> — limit sell: books exactly ${fmtPx(tp)} once the price reaches it.`);
      }
      lines.push(`<b>Stop</b> — sells at ${fmtPx(sl)}, or at the market if the price is already below it (never booked more than 4% under the stop).`);
      lines.push(`<b>Time</b> — if neither hits first, sold at the market early on ${esc(rec.target_exit_date || '?')}.`);
    } else {
      // A stop is checked on the engine's next look, so a gap down fills
      // under it — the reason losses run past the stop. Say by how much.
      const exitPx = num(rec.exit_price);
      const slip = (rec.reason === 'SL' && sl != null && exitPx != null && exitPx < sl - 1e-9)
        ? ` Filled ${fmtPx(exitPx)}, ${(Math.abs(exitPx / sl - 1) * 100).toFixed(2)}% under the planned stop: the price was already below it when the engine looked.`
        : '';
      lines.push(`<b>Exit</b> — ${esc(EXIT_TEXT[rec.reason] || rec.reason || 'reason not recorded')}.${slip}`);
      if (planNote) lines.push(esc(planNote));
    }
    const cost = num(rec.cost_sek) != null ? num(rec.cost_sek) : shares * (entry || 0);
    const fill = fillText(spec.engine, rec);
    lines.push(`<b>Position</b> — ${fmt(shares, 0)} shares · ${fmt(cost, 0)} SEK · bought `
             + esc(tsFull(rec.entry_ts) || rec.entry_date || '?') + (fill ? ` — ${esc(fill)}` : '') + '.');

    // ── why it was bought ───────────────────────────────────────────────────
    const builder = WHY[spec.engine];
    const checks = builder ? builder(si, rec) : [];
    let rules;
    if (!ver) rules = '<span class="lwm-muted">No strategy version was stamped on this trade.</span>';
    else if (ref.state === 'ok') {
      const row = verRow(spec.engine, ver);
      rules = row && row.params
        ? `<span class="lwm-rules-k">Rules at entry · ${esc(ver)}</span> ${esc(row.params)}`
        : `<span class="lwm-muted">${esc(ver)} is not in the changelog.</span>`;
    } else if (ref.state === 'failed') {
      rules = '<span class="lwm-muted">Strategy rules unavailable — the changelog did not load.</span>';
    } else {
      rules = '<span class="lwm-muted">Loading the strategy rules…</span>';
    }

    return `
      ${flag}
      <div class="lwm-kicker"><span class="lwm-dot" style="background:${meta.color};"></span>${esc(meta.label)} · ${isOpen ? 'Open position' : 'Closed trade'}</div>
      <div class="lwm-title" id="lwm-title">${esc(tk)}</div>
      ${pills ? `<div class="lwm-pills">${pills}</div>` : ''}
      <div class="lwm-status">${statusLine}</div>

      <div class="lwm-tiles">${tiles}</div>
      ${ladderHtml(entry, tp, sl, mark, markLbl)}
      <div class="lwm-lines">${lines.map(l => `<div>${l}</div>`).join('')}</div>

      <div class="lwm-sec">
        <div class="lwm-sec-title">Why it was bought</div>
        <div class="lwm-lead">${esc(LEAD[spec.engine] || '')}</div>
        ${checks.length ? checks.map(checkHtml).join('')
          : '<div class="lwm-muted">The engine recorded no entry figures for this trade.</div>'}
        <div class="lwm-rules">${rules}</div>
      </div>

      ${trackRecordHtml(spec, f, si)}

      <div class="lwm-foot">
        <a class="lwm-btn${stockBtn ? '' : ' primary'}" href="${meta.link}">${esc(meta.label)} page →</a>
        ${stockBtn ? `<a class="lwm-btn primary" href="/view?path=${encodeURIComponent(path)}">Stock page →</a>` : ''}
      </div>`;
  }

  // ── stock page: every live-engine trade in this stock ──────────────────────
  // stock_template.html carries <aside id="live-trades" data-insref data-name>.
  // It is filled here, from the same four wallets on the same poll as the
  // widget, so the rail, the widget and the detail window can never disagree,
  // and a row opens the same detail window.
  //
  // Trades are matched on insref, not on the name. A closed trade carries only
  // its display name, which the wallet's ticker_paths maps to a folder ending
  // in {insref}_{name} — so a share that was renamed between trades (Munters
  // Group / Munters Group AB) still lands on one page. Names with no path fall
  // back to an exact name match. Where the rail sits — fixed beside the content
  // on wide screens, a card under the chart otherwise — is the template's CSS.

  const quoteOf = (w, tk) => {
    const q = w.last_quotes && w.last_quotes[tk];
    return (q && q.last != null && isFinite(q.last)) ? { last: Number(q.last), stale: q.source === 'stale' } : null;
  };

  function railTrades(insref, name) {
    const items = [], missing = [];
    for (const [key, meta] of Object.entries(ENGINES)) {
      const w = lastWallets && lastWallets[key];
      if (!w || w.__unavailable) { missing.push(meta.label); continue; }
      const paths = (w.ticker_paths && typeof w.ticker_paths === 'object') ? w.ticker_paths : {};
      const isThis = tk => {
        const m = /\/(\d+)_[^/]*$/.exec(paths[tk] || '');
        return m ? Number(m[1]) === insref : tk === name;
      };
      const positions = (w.positions && typeof w.positions === 'object') ? w.positions : {};
      for (const [tk, pos] of Object.entries(positions)) {
        // An open position stores its own insref; trust that over the path map.
        if (pos.insref != null ? Number(pos.insref) === insref : isThis(tk)) {
          items.push({ kind: 'open', key, tk, rec: pos, w });
        }
      }
      for (const t of (Array.isArray(w.closed_trades) ? w.closed_trades : [])) {
        if (t && t.ticker && isThis(t.ticker)) items.push({ kind: 'closed', key, tk: t.ticker, rec: t, w });
      }
    }
    // Newest purchase first. ISO stamps sort lexicographically.
    const bought = it => String(it.rec.entry_ts || it.rec.entry_date || '');
    items.sort((a, b) => bought(b).localeCompare(bought(a)));
    return { items, missing };
  }

  function railRowHtml(it) {
    const meta = ENGINES[it.key];
    const rec = it.rec, isOpen = it.kind === 'open';
    const entry = num(rec.entry_price);
    const shares = num(rec.shares) || 0;
    let px, ret, pnl, tag, tagCls, stale = false;
    if (isOpen) {
      const q = quoteOf(it.w, it.tk);
      px = q ? q.last : null;
      stale = !!(q && q.stale);
      ret = (px != null && entry) ? px / entry - 1 : null;
      pnl = (px != null && entry) ? shares * (px - entry) : null;
      tag = 'OPEN'; tagCls = 'reason-OPEN';
    } else {
      px = num(rec.exit_price); ret = num(rec.ret_pct); pnl = num(rec.pnl_sek);
      tag = REASON_SHORT[rec.reason] || rec.reason || '?';
      tagCls = `reason-${rec.reason}`;
    }
    const entryTs = rec.entry_ts || rec.entry_date || '';
    const exitTs  = rec.exit_ts || rec.exit_date || '';
    const day = ts => String(ts || '').slice(0, 10);
    const held = isOpen ? daysBetween(rec.entry_date, localIso()) : daysBetween(rec.entry_date, rec.exit_date);
    const heldTxt = held == null ? '' : held <= 0 ? (isOpen ? 'bought today' : 'same day') : plural(held, 'day');
    const when = isOpen
      ? `bought ${day(entryTs)}${rec.target_exit_date ? ` · exit by ${rec.target_exit_date}` : ''}`
      : (day(entryTs) === day(exitTs) ? day(entryTs) : `${day(entryTs)} → ${day(exitTs)}`)
        + (heldTxt ? ` · ${heldTxt}` : '');
    const title = `${meta.label} · ${fmt(shares, 0)} sh`
      + `\nBought ${tsLong(entryTs)} @ ${fmtPx(entry)}`
      + (isOpen
          ? (px != null ? `\nNow ${fmtPx(px)}${stale ? ' (stale quote)' : ''}` : '\nNo live quote yet')
          : `\nSold ${tsLong(exitTs)} @ ${fmtPx(px)} · ${rec.reason || ''}`)
      + CLICK_HINT;
    const spec = { engineKey: it.key, ticker: it.tk, entryTs, exitTs };
    return `
      <a class="lt-row" href="${meta.link}" title="${esc(title)}" ${rowAttrs(it.kind, spec)}>
        <span class="lt-r1">
          <span class="lt-dot" style="background:${meta.color};"></span>
          <span class="lt-eng">${esc(meta.label)}</span>
          <span class="lt-ver">${esc(rec.strategy_version || '')}</span>
          <span class="lt-ret ${ret == null ? '' : cls(ret)}">${pctTxt(ret)}</span>
          <span class="lt-tag ${esc(tagCls)}">${esc(tag)}</span>
        </span>
        <span class="lt-r2">
          <span>${entry == null ? '—' : fmt(entry, 2)} → ${px == null ? '—' : fmt(px, 2)}${isOpen ? ' now' : ''}${stale ? ' <span class="q-stale">stale</span>' : ''}</span>
          <span class="${pnl == null ? '' : cls(pnl)}">${pnl == null ? '—' : sign(pnl) + ' SEK'}</span>
        </span>
        <span class="lt-r3">${esc(when)}</span>
      </a>`;
  }

  function renderRail() {
    const rail = document.getElementById('live-trades');
    if (!rail || !lastWallets) return;
    const body  = rail.querySelector('.lt-body');
    const count = rail.querySelector('.lt-count');
    if (!body) return;
    const insref = Number(rail.dataset.insref);
    const name   = rail.dataset.name || '';
    if (!insref) {
      body.innerHTML = '<div class="lt-empty">This page has no instrument id, so its live trades cannot be matched.</div>';
      return;
    }
    const { items, missing } = railTrades(insref, name);
    const all = Object.keys(ENGINES).length;
    // Same rule as the widget: a wallet that did not answer is named, never
    // shown as "no trades".
    const warn = !missing.length ? ''
      : missing.length === all
        ? '<div class="lt-warn">⚠ No wallet answered — the app may be restarting</div>'
        : `<div class="lt-warn">⚠ ${esc(missing.join(', '))} did not answer — ${missing.length > 1 ? 'their' : 'its'} trades are missing below</div>`;
    if (count) count.textContent = items.length ? `· ${items.length}` : '';
    const more = rail.querySelector('.lt-note-more');   // "click one…" needs rows
    if (more) more.hidden = !items.length;
    if (!items.length) {
      body.innerHTML = warn + (missing.length === all ? ''
        : `<div class="lt-empty">No live engine has traded ${esc(name || 'this stock')}${missing.length ? ' in the wallets that answered' : ''}.</div>`);
      return;
    }

    // Summary: realised over the closed trades (STALE exits count in P&L but
    // not in the win rate, as everywhere else), unrealised over what is held.
    const closed = items.filter(i => i.kind === 'closed').map(i => i.rec);
    const open   = items.filter(i => i.kind === 'open');
    const r = record(closed);
    let unreal = 0, quoted = 0;
    for (const o of open) {
      const q = quoteOf(o.w, o.tk), e = num(o.rec.entry_price);
      if (q && e) { unreal += (num(o.rec.shares) || 0) * (q.last - e); quoted += 1; }
    }
    const blocks = [closed.length
      ? `<div class="lt-block"><div class="lt-lbl">Realised · ${plural(closed.length, 'closed trade')}</div>`
        + `<div class="lt-val ${cls(r.pnl)}">${sign(r.pnl)} <span class="lt-unit">SEK</span></div>`
        + `<div class="lt-sub">${r.wins}W / ${r.n - r.wins}L`
        + (r.wr != null ? ` · ${Math.round(r.wr * 100)}% won` : '')
        + (r.avg != null ? ` · avg ${pctTxt(r.avg)}` : '')
        + (r.all > r.n ? ` · ${r.all - r.n} STALE unscored` : '') + `</div></div>`
      : '<div class="lt-block"><div class="lt-lbl">Realised</div><div class="lt-sub">no closed trades yet</div></div>'];
    if (open.length) {
      blocks.push(`<div class="lt-block"><div class="lt-lbl">Held now · ${open.length}</div>`
        + `<div class="lt-val ${quoted ? cls(unreal) : ''}">${quoted ? sign(unreal) : '—'} <span class="lt-unit">SEK</span></div>`
        + `<div class="lt-sub">unrealised${quoted < open.length ? ' · some have no quote yet' : ''}</div></div>`);
    }

    // One chip per engine that has touched the stock: trade count + realised.
    const per = {};
    for (const it of items) {
      const p = per[it.key] || (per[it.key] = { n: 0, open: 0, pnl: 0 });
      p.n += 1;
      if (it.kind === 'open') p.open += 1;
      else p.pnl += num(it.rec.pnl_sek) || 0;
    }
    const chips = Object.keys(ENGINES).filter(k => per[k]).map(k => {
      const m = ENGINES[k], p = per[k];
      const tip = `${m.label}: ${plural(p.n, 'trade')}${p.open ? `, ${p.open} open` : ''}`
                + (p.n > p.open ? ` · ${sign(p.pnl)} SEK realised` : '');
      return `<a class="lt-chip" href="${m.link}" title="${esc(tip)}">`
           + `<span class="lt-dot" style="background:${m.color};"></span>${esc(m.label)} ${p.n}`
           + (p.n > p.open ? ` <b class="${cls(p.pnl)}">${sign(p.pnl)}</b>` : '') + `</a>`;
    }).join('');

    body.innerHTML = warn
      + `<div class="lt-sum">${blocks.join('')}</div>`
      + `<div class="lt-engines">${chips}</div>`
      + `<div class="lt-list">${items.map(railRowHtml).join('')}</div>`;
  }
})();
