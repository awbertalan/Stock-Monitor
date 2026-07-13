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
