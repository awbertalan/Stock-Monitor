// live_widget.js — floating left-side summary of all three Live engines.
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

  async function refresh() {
    try {
      const [sig, pat, crs] = await Promise.all([
        fetch('/live-portfolio').then(r => r.json()).catch(() => ({})),
        fetch('/live-patterns-portfolio').then(r => r.json()).catch(() => ({})),
        fetch('/live-cross-portfolio').then(r => r.json()).catch(() => ({})),
      ]);
      render({ signals: sig, patterns: pat, cross: crs });
    } catch (e) {
      // Quietly fail — the user is probably on a page that's working fine
      // without the widget; don't spam console.
    }
  }

  // Engine identity: shared across summarise() and the open-positions render
  // so colours and dot labels stay in lockstep.
  const ENGINES = {
    signals:  { label: 'Signals',  link: '/live-signals',  color: '#0052cc' },
    patterns: { label: 'Patterns', link: '/live-patterns', color: '#1a8042' },
    cross:    { label: 'Cross',    link: '/live-cross',    color: '#b08000' },
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
        const last = (eng.quotes[tk] && eng.quotes[tk].last) || pos.entry_price;
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
          ret,
          pnl,
        });
      }
    }
    rows.sort((a, b) => b.pnl - a.pnl);
    return rows;
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
  function renderPositionRows(d) {
    const rows = collectOpenPositions({
      signals:  summarise(d.signals),
      patterns: summarise(d.patterns),
      cross:    summarise(d.cross),
    });
    if (!rows.length) return '';
    return `<div class="lw-positions">` + rows.map(r => {
      const retPct = (r.ret * 100).toFixed(2);
      const retStr = `${r.ret >= 0 ? '+' : ''}${retPct}%`;
      const title  = `${r.engine} · ${fmt(r.shares, 0)} sh @ ${fmt(r.entry, 2)} → ${fmt(r.last, 2)}  ·  ${sign(r.pnl)} SEK`;
      const tkDisp = r.ticker.length > 18 ? r.ticker.slice(0, 17) + '…' : r.ticker;
      return `
        <a class="lw-pos" href="${r.stockLink}" title="${title.replace(/"/g, '&quot;')}">
          <span class="lw-dot" style="background:${r.color};"></span>
          <span class="lw-pos-tk">${tkDisp}</span>
          <span class="lw-pos-ret ${cls(r.ret)}">${retStr}</span>
        </a>`;
    }).join('') + `</div>`;
  }

  function render(d) {
    const s = summarise(d.signals);
    const p = summarise(d.patterns);
    const x = summarise(d.cross);
    const all = [s, p, x];
    const combinedEquity = all.reduce((a, e) => a + e.equity, 0);
    const combinedOpen = all.reduce((a, e) => a + e.openCount, 0);
    const combinedTrades = all.reduce((a, e) => a + e.total, 0);
    const combinedWins = all.reduce((a, e) => a + e.wins, 0);
    const wr = combinedTrades ? combinedWins / combinedTrades : null;
    const td = todayPnL([s.closed, p.closed, x.closed]);
    const startCapital = 1_500_000;
    const equityPct = (combinedEquity / startCapital - 1) * 100;

    const body = document.getElementById('live-widget-body');
    if (!body) return;
    body.innerHTML = `
      <div class="lw-block">
        <div class="lw-lbl">Equity</div>
        <div class="lw-val">${fmt(combinedEquity, 0)} <span class="lw-unit">SEK</span></div>
        <div class="lw-sub ${cls(equityPct)}">${equityPct >= 0 ? '+' : ''}${equityPct.toFixed(2)}% vs 1500k</div>
      </div>
      <div class="lw-block">
        <div class="lw-lbl">Today's P&amp;L</div>
        <div class="lw-val ${cls(td.pnl)}">${sign(td.pnl)} <span class="lw-unit">SEK</span></div>
        <div class="lw-sub">${td.count} closed today</div>
      </div>
      <div class="lw-block">
        <div class="lw-lbl">Open positions · ${combinedOpen}</div>
        ${renderPositionRows(d) || `<div class="lw-empty">No open positions</div>`}
        <div class="lw-engine-counts" title="By engine">
          <a class="lw-mini" href="/live-signals" title="Signals — ${s.openCount} open">
            <span class="lw-dot" style="background:${ENGINES.signals.color};"></span>${s.openCount}
          </a>
          <a class="lw-mini" href="/live-patterns" title="Patterns — ${p.openCount} open">
            <span class="lw-dot" style="background:${ENGINES.patterns.color};"></span>${p.openCount}
          </a>
          <a class="lw-mini" href="/live-cross" title="Cross — ${x.openCount} open">
            <span class="lw-dot" style="background:${ENGINES.cross.color};"></span>${x.openCount}
          </a>
        </div>
      </div>
      <div class="lw-block">
        <div class="lw-lbl">Win rate</div>
        <div class="lw-val">${wr === null ? '—' : (wr * 100).toFixed(0) + '%'}</div>
        <div class="lw-sub">${combinedWins}W / ${combinedTrades - combinedWins}L · ${combinedTrades} total</div>
      </div>
    `;
  }
})();
