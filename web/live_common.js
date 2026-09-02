// Shared display helpers for the live-engine pages (live_signals, live_patterns,
// live_cross, live_overview). Loaded as a plain <script> *before* each page's
// inline script, so these become page-level globals — previously every page
// carried its own byte-identical copy of this block.

const fmt   = (n, d = 0) => Number(n).toLocaleString('sv-SE', { maximumFractionDigits: d, minimumFractionDigits: d });
// Compact-format an ISO timestamp "2026-05-19T10:23:45" -> "2026-05-19 10:23:45"
const fmtTs = ts => ts ? String(ts).replace('T', ' ').slice(0, 19) : '';
const pct   = (n, d = 2) => `${n >= 0 ? '+' : ''}${(n * 100).toFixed(d)}%`;
const sign  = n => n >= 0 ? `+${fmt(n, 0)}` : fmt(n, 0);
const cls   = n => n > 0 ? 'up' : (n < 0 ? 'down' : 'neutral');
const esc   = s => String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const SELL_MIN_RET = 0.0075;   // UI gate: sell button unlocks at ≥+0.75% unrealized

// Render a ticker as a link to its stock page when we know its CSV path, else
// plain (escaped) text. Reads the page-global `tickerPaths` map if present.
function tickerLink(name) {
  const paths = (typeof tickerPaths !== 'undefined') ? tickerPaths : null;
  const path = paths ? paths[name] : null;
  if (!path) return esc(name);
  return `<a href="/view?path=${encodeURIComponent(path)}" class="ticker-link">${esc(name)}</a>`;
}

// ── engine health banner ─────────────────────────────────────────────────────
// Two multi-week outages ran completely unnoticed: the engines were dark from
// 2026-06-16 to 2026-07-28, and the EOD sweep + daily OHLC capture were blocked
// by a 6-second clock bug from 2026-06-13 to 2026-08-18. In both cases the pages
// kept rendering a healthy-looking wallet. Every live page now polls /live-health
// and puts anything degraded at the top, where it cannot be missed.

function renderHealthBanner(h) {
  let el = document.getElementById('health-banner');
  const bad = [...h.engines, ...h.jobs].filter(i => i.status !== 'ok');
  if (!bad.length) { if (el) el.remove(); return; }

  if (!el) {
    el = document.createElement('div');
    el.id = 'health-banner';
    const nav = document.querySelector('nav');
    if (nav && nav.parentNode) nav.parentNode.insertBefore(el, nav.nextSibling);
    else document.body.insertBefore(el, document.body.firstChild);
  }
  el.className = `health-banner ${h.worst}`;
  const noun = bad.length === 1 ? 'component is' : 'components are';
  el.innerHTML =
    `<div class="hb-title">⚠ ${bad.length} ${noun} not healthy</div>` +
    `<ul>${bad.map(i => `<li><span class="hb-name">${esc(i.name)}</span> — ${esc(i.detail)}</li>`).join('')}</ul>`;
}

function refreshHealth() {
  fetch('/live-health')
    .then(r => r.json())
    .then(renderHealthBanner)
    .catch(() => {});
}

document.addEventListener('DOMContentLoaded', () => {
  refreshHealth();
  setInterval(refreshHealth, 60_000);
});

// ── trade scoring ────────────────────────────────────────────────────────────
// All three engines run TP +2.0% / SL -2.0% — a 1:1 bet, so the strategy only
// pays above a 50% win rate. The pages used to hard-code three *different*
// thresholds: 80% on Signals (left over from the 0.5%/-2.0% strategy retired
// 2026-05-23) and 33% on Patterns (left over from the +2/-1 stop, widened
// 2026-05-25). Patterns' 50.9% WR rendered as a comfortable pass against its
// stale 33% label when it was in fact sitting exactly on the break-even line.
// Signals moved to SL -1.0% on 2026-08-24 while Patterns and Cross stayed at
// -2.0%, so this can no longer be one shared number. Each page declares its own
// engine's pair via setBreakeven(); the 0.50 default is the -2.0% case, so a
// page that forgets to call it still gets the stricter bar rather than a
// flattering one. `let`, not `const`, precisely so a page can override it.
let BREAKEVEN_WR = 0.50;

// |SL| / (TP + |SL|) — the win rate at which expectancy is zero.
const setBreakeven = (tp, sl) => (BREAKEVEN_WR = Math.abs(sl) / (tp + Math.abs(sl)));

const wrClass = wr =>
  wr === null || wr === undefined ? ''
    : (wr > BREAKEVEN_WR ? 'up' : (wr < BREAKEVEN_WR ? 'down' : 'neutral'));

// Positions the engine was not running to manage, booked STALE past their exit
// date (see STALE_EXIT_GRACE_DAYS in live_signals.py). The cash moved for real
// so they stay in realised P&L, but their exit price reflects whenever the
// engine came back rather than any strategy rule — so they must not score.
const scoredTrades = list => (list || []).filter(t => t.reason !== 'STALE');

// ── cap tier ─────────────────────────────────────────────────────────────────
// stock_names.csv carries the Stockholm cap tier (large / mid / small cap) in
// its last column. live_cross had a Tier column first, fed by a value its
// engine stamped onto each candidate ("large cap" / "mid/small"); serving the
// map straight from the CSV gives every live page the same pill — at full
// precision, and without each engine having to plumb a tier through its state.
let tierMap = {};
let _tierPromise = null;

// Fetch once per page load; safe to await from every refresh().
function loadTiers() {
  if (!_tierPromise) {
    _tierPromise = fetch('/live-tiers')
      .then(r => r.json())
      .then(m => { tierMap = (m && typeof m === 'object') ? m : {}; })
      .catch(() => { _tierPromise = null; });
  }
  return _tierPromise;
}

// `fallback` covers names the CSV has no tier for (delisted, renamed, or a
// venue outside the Nordic cap lists) — live_cross passes its engine value.
function tierPill(name, fallback) {
  return `<span class="pill tier-pill">${esc(tierMap[name] || fallback || '?')}</span>`;
}

const tierCell = (name, fallback) => `<td>${tierPill(name, fallback)}</td>`;

// ── sector ───────────────────────────────────────────────────────────────────
// Column 5 of stock_names.csv, backfilled by scripts/tag_sectors.py from the
// feed's numeric `sector`/`sectorname` pair. Added 2026-08-31: on that session
// four of the day's six exits were property names that stopped out together,
// and nothing in the project stored a sector, so a book with 4 correlated
// positions rendered identically to one with 4 unrelated ones.
//
// Informational only. No engine gates on it.
let sectorMap = {};
let _sectorPromise = null;

function loadSectors() {
  if (!_sectorPromise) {
    _sectorPromise = fetch('/live-sectors')
      .then(r => r.json())
      .then(m => { sectorMap = (m && typeof m === 'object') ? m : {}; })
      .catch(() => { _sectorPromise = null; });
  }
  return _sectorPromise;
}

const sectorOf = name => sectorMap[name] || '';

function sectorPill(name) {
  const s = sectorOf(name);
  return s ? `<span class="pill sector-pill">${esc(s)}</span>` : '';
}

// [{sector, tickers, n, cost, pct}] heaviest first — mirrors
// engine_common.sector_concentration so the page and the log agree.
function sectorConcentration(positions) {
  const groups = {};
  let total = 0;
  (positions || []).forEach(p => {
    const sec  = sectorOf(p.ticker) || '(unknown)';
    const cost = Number(p.cost) || 0;
    (groups[sec] = groups[sec] || { sector: sec, tickers: [], n: 0, cost: 0 });
    groups[sec].tickers.push(p.ticker);
    groups[sec].n += 1;
    groups[sec].cost += cost;
    total += cost;
  });
  return Object.values(groups)
    .map(g => ({ ...g, pct: total ? (g.cost / total) * 100 : 0 }))
    .sort((a, b) => b.cost - a.cost || b.n - a.n);
}

// ── version stamps ───────────────────────────────────────────────────────────
// Every position the engines open is stamped with the app version (which build
// of the code) and the strategy version (which generation of tradeable
// parameters) — see lib/version.py. Only the strategy tier is worth grouping
// performance by: an app version bumps on CSS tweaks too, which would scatter
// ~100 trades across dozens of buckets with n<=2 each.
//
// The pill answers the question the 2026-08-18 review had to answer by hand:
// which code was live when this trade was opened. The -1% -> -2% stop widening
// on 2026-05-25 moved break-even WR from 33% to 50% and took an audit to find.

let verLedger = null;
let _verPromise = null;

function loadVersions() {
  if (!_verPromise) {
    _verPromise = fetch('/changelog-data')
      .then(r => r.json())
      .then(d => { verLedger = d && !d.error ? d : null; })
      .catch(() => { _verPromise = null; });
  }
  return _verPromise;
}

// The strategy_history row for engine+version, for tooltips.
function verInfo(engine, ver) {
  const hist = verLedger && verLedger.strategy_history ? verLedger.strategy_history[engine] : null;
  if (!hist) return null;
  return hist.find(r => r.version === ver) || null;
}

// Render one trade's / position's strategy version as a pill linking to the
// changelog. `rec` is a closed_trade or position dict.
function verPill(engine, rec) {
  const ver = rec && rec.strategy_version;
  if (!ver) return `<span class="pill ver-pill none" title="Opened before version tracking existed, and its entry date predates this engine's recorded history.">—</span>`;
  const info    = verInfo(engine, ver);
  const current = verLedger && verLedger.strategy_versions
                && verLedger.strategy_versions[engine] === ver;
  const back    = rec.version_source === 'backfilled';
  const bits = [];
  if (info && info.params) bits.push(info.params);
  if (info && info.from)   bits.push(`live from ${info.from}`);
  if (rec.app_version)     bits.push(`app v${rec.app_version}`);
  if (info && info.note)   bits.push(info.note);
  bits.push(back
    ? 'Version INFERRED from entry date — see scripts/backfill_versions.py'
    : 'Version stamped by the engine at entry');
  const klass = `pill ver-pill${back ? ' backfilled' : ''}${current ? ' current' : ''}`;
  return `<a href="/changelog#${encodeURIComponent(ver)}" class="${klass}" `
       + `title="${esc(bits.join('\n'))}">${esc(ver)}${back ? '*' : ''}</a>`;
}

const verCell = (engine, rec, tdClass = '') =>
  `<td class="${tdClass}">${verPill(engine, rec)}</td>`;

// "By version" analytics panel — the payoff of stamping. Groups closed trades
// by strategy version so a parameter change can be judged against the one
// before it instead of re-audited by hand.
//
// Scoring matches the rest of the page: realised P&L counts every exit, WR and
// average return count scoredTrades() only (STALE exits are outage artefacts).
function renderVersionBreakdown(engine, closed, bodyId) {
  const body = document.getElementById(bodyId);
  if (!body) return;
  const groups = new Map();
  (closed || []).forEach(t => {
    const k = t.strategy_version || 'unversioned';
    if (!groups.has(k)) groups.set(k, []);
    groups.get(k).push(t);
  });

  // Chronological by the version's own start date, so the table reads as a
  // timeline; unknown versions sort last.
  const order = v => {
    const i = verInfo(engine, v);
    return i && i.from ? i.from : '9999';
  };
  const rows = [...groups.keys()].sort((a, b) => order(a).localeCompare(order(b)))
    .map(v => {
      const all    = groups.get(v);
      const scored = scoredTrades(all);
      const n      = scored.length;
      const wins   = scored.filter(t => (t.pnl_sek || 0) > 0).length;
      const wr     = n ? wins / n : null;
      const avgRet = n ? scored.reduce((s, t) => s + (t.ret_pct || 0), 0) / n : null;
      const total  = all.reduce((s, t) => s + (t.pnl_sek || 0), 0);
      const info   = verInfo(engine, v);
      const excl   = all.length - n;
      return `
        <tr>
          <td>${verPill(engine, all[0])}</td>
          <td class="num" title="${excl ? esc(excl + ' STALE exit(s) excluded from WR / avg ret') : ''}">${n}${excl ? `<span class="ver-excl">+${excl}</span>` : ''}</td>
          <td class="num ${wr === null ? '' : wrClass(wr)}">${wr === null ? '—' : (wr*100).toFixed(0) + '%'}</td>
          <td class="num ${avgRet === null ? '' : cls(avgRet)}">${avgRet === null ? '—' : pct(avgRet, 2)}</td>
          <td class="num ${cls(total)}">${sign(total)}</td>
          <td class="mobile-hide ver-note">${info ? esc(info.note) : ''}</td>
        </tr>`;
    }).join('');
  body.innerHTML = rows || '<tr><td colspan="6" class="empty-row">No closed trades yet</td></tr>';
}
