// live_widget.js — floating left-side summary of all four Live engines.
// Renders on every page except /live*  (the user is already there).
// Auto-suppressed on viewports narrower than 1280px so it never overlaps
// the centred page content.

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
  function attach() { document.body.appendChild(root); refresh(); setInterval(refresh, 60_000); }
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
    try {
      const [sig, pat, crs, brk] = await Promise.all([
        loadWallet('/live-portfolio'),
        loadWallet('/live-patterns-portfolio'),
        loadWallet('/live-cross-portfolio'),
        loadWallet('/live-breakout-portfolio'),
      ]);
      render({ signals: sig, patterns: pat, cross: crs, breakout: brk });
    } catch (e) {
      // Rendering itself failed. loadWallet() already absorbs the fetch side,
      // so getting here means a bug in this file — say so on screen instead of
      // leaving the last (now stale) numbers standing as if they were current.
      const body = document.getElementById('live-widget-body');
      if (body) body.innerHTML = `<div class="lw-empty">Live widget error — see console</div>`;
      console.warn('[live_widget] render failed:', e);
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

  function collectClosedTrades(engineSummaries) {
    const rows = [];
    for (const [key, eng] of Object.entries(engineSummaries)) {
      const meta = ENGINES[key];
      for (const t of eng.closed) {
        const path = eng.ticker_paths[t.ticker] || null;
        rows.push({
          ticker: t.ticker,
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
                  + `${sign(r.pnl)} SEK`;
      return `
        <a class="lw-trade" href="${r.stockLink}" title="${title.replace(/"/g, '&quot;')}">
          <span class="lw-trade-top">
            <span class="lw-dot" style="background:${r.color};"></span>
            <span class="lw-trade-tk">${r.ticker}</span>
            <span class="lw-trade-ret ${cls(r.ret)}">${r.ret >= 0 ? '+' : ''}${(r.ret * 100).toFixed(2)}%</span>
            <span class="lw-trade-tag reason-${r.reason}">${tag}</span>
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

  // Render the open-position detail rows. Each row links to the stock's
  // detail page if we have a ticker_path; otherwise falls back to the
  // engine's tab. Sorted winners-first via collectOpenPositions.
  function renderPositionRows(summaries) {
    const rows = collectOpenPositions(summaries);
    if (!rows.length) return '';
    return `<div class="lw-positions">` + rows.map(r => {
      const retPct = (r.ret * 100).toFixed(2);
      const retStr = `${r.ret >= 0 ? '+' : ''}${retPct}%`;
      const title  = `${r.engine} · ${fmt(r.shares, 0)} sh @ ${fmt(r.entry, 2)} → ${fmt(r.last, 2)}  ·  ${sign(r.pnl)} SEK`
                   + (r.entryTs ? `\nBought ${tsLong(r.entryTs)}` : '')
                   + (r.stale ? `\nStale quote — previous good price, P&amp;L may be behind` : '');
      // Same marker + wording as live_common.js staleMark(), and the same
      // .q-stale token styling, so a stale row reads identically here and on
      // the live pages.
      const staleTag = r.stale
        ? ` <span class="q-stale" title="Live quote unavailable on the last run — this is the previous good price, so P&amp;L may be behind">stale</span>`
        : '';
      const tkDisp = r.ticker.length > 18 ? r.ticker.slice(0, 17) + '…' : r.ticker;
      return `
        <a class="lw-pos" href="${r.stockLink}" title="${title.replace(/"/g, '&quot;')}">
          <span class="lw-dot" style="background:${r.color};"></span>
          <span class="lw-pos-tk">${tkDisp}</span>${staleTag}
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
})();
