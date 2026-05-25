// stock_page.js — shared chart engine loaded by each stock's index.html
// Requires:  const STOCK = { name, insref, type, currency, csv7d };

const PAD = { top: 16, right: 16, bottom: 42, left: 72 };
const VOL_BAND_H = 60;   // height of the volume band overlaid at the bottom of the price chart
const priceBot = H => H - PAD.bottom - (showVolume ? VOL_BAND_H : 0);

let allData          = [];
let viewStart        = 0;
let viewEnd          = 0;
let showVolume       = false;
let showCandles      = false;
let candleBucketMs   = null;   // null = auto-select by view range
let measureState     = null;  // { startX, startIdx } during drag
let measureRange     = null;  // { startIdx, endIdx } persists after drag
let hoverIdx         = null;
const showMA         = { 20: false, 50: false, 200: false };
const MA_COLORS      = { 20: '#f0a500', 50: '#0066cc', 200: '#cc3300' };
let showRSI          = false;
let currentCsvPath   = null;
let usingFullHistory = false;
let _lastCandles     = [];

const RANGE_BTNS = { 1:'btn-1d', 3:'btn-3d', 7:'btn-7d', 30:'btn-1m', 91:'btn-3m', 182:'btn-6m', 365:'btn-1y' };

// ── Bootstrap ─────────────────────────────────────────────────────────────────

async function loadData() {
  currentCsvPath = STOCK.csv7d;
  try {
    await loadCsv(currentCsvPath, true);
    let intervalMs = 30000;
    try {
      const s = await fetch('/get-settings').then(r => r.json());
      const sec = parseInt(s.refresh_interval_s, 10);
      intervalMs = isNaN(sec) ? 30000 : sec * 1000;
    } catch { /* keep default */ }
    if (intervalMs > 0) setInterval(refreshData, intervalMs);
  } catch {
    document.getElementById('change').textContent = 'Failed to load data.';
  }
  initWatchlist();
  loadAlertState();
  loadRecommendations();
  loadQuote();
  loadOrderBook();
  loadNews();
  loadTrades();
}

async function loadCsv(path, init = false) {
  const res  = await fetch('/csv?path=' + encodeURIComponent(path));
  const data = await res.json();
  if (!Array.isArray(data) || !data.length) throw new Error('empty');
  allData      = data;
  viewStart    = 0;
  viewEnd      = allData.length - 1;
  measureRange = null;
  updateTicker();
  if (init) initChart(); else redraw();
  if (_patternMode) runPatternDetection();
}

// ── Ticker ────────────────────────────────────────────────────────────────────

function updateTicker() {
  const data   = allData.slice(viewStart, viewEnd + 1);
  const prices = data.map(r => r[1]);
  const vols   = data.map(r => r[2] || 0);
  const first  = prices[0];
  const last   = prices[prices.length - 1];
  const change = last - first;
  const pct    = (change / first) * 100;
  const isUp   = change >= 0;
  const color  = isUp ? '#00b36b' : '#e03131';

  document.getElementById('price').textContent = last.toFixed(2);

  const chEl = document.getElementById('change');
  chEl.textContent = `${isUp ? '▲' : '▼'} ${change >= 0 ? '+' : ''}${change.toFixed(2)}  (${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%)`;
  chEl.style.color = color;

  document.getElementById('s-open').textContent = first.toFixed(2);
  document.getElementById('s-high').textContent = Math.max(...prices).toFixed(2);
  document.getElementById('s-low').textContent  = Math.min(...prices).toFixed(2);
  document.getElementById('s-vol').textContent  = fmtVol(vols.reduce((a, b) => a + b, 0));
}

// ── Chart init ────────────────────────────────────────────────────────────────

function initChart() {
  const pc = document.getElementById('price-chart');
  const rc = document.getElementById('rsi-chart');
  const W  = pc.offsetWidth || 760;
  pc.width  = W;  pc.height  = 320;
  if (rc) { rc.width = W; rc.height = 80; }
  setupEvents(pc);
  redraw();
}

function redraw() {
  if (showCandles) drawCandles();
  else drawPrice();
  if (showRSI) drawRSI();
  if (_patternMode && _lastCandles.length) drawPatternOverlay();
}

function visData() {
  return allData.slice(viewStart, viewEnd + 1);
}

// ── Price chart ───────────────────────────────────────────────────────────────

function drawPrice() {
  _lastCandles = [];   // no candle markers in line mode
  const canvas = document.getElementById('price-chart');
  const ctx    = canvas.getContext('2d');
  const W = canvas.width, H = canvas.height;
  const data = visData();
  if (!data.length) return;

  const prices = data.map(r => r[1]);
  const minP   = Math.min(...prices);
  const maxP   = Math.max(...prices);
  const pRange = maxP - minP || 1;
  const n      = data.length;
  const isUp   = prices[prices.length - 1] >= prices[0];
  const color  = isUp ? '#00b36b' : '#e03131';

  const pBot = priceBot(H);
  const xOf = i => PAD.left + (i / Math.max(n - 1, 1)) * (W - PAD.left - PAD.right);
  const yOf = p => PAD.top  + (1 - (p - minP) / pRange) * (pBot - PAD.top);
  const barW = Math.max(1, (W - PAD.left - PAD.right) / n * 0.7);

  ctx.clearRect(0, 0, W, H);
  ctx.font = '11px Arial';

  // Grid + Y labels
  for (let g = 0; g <= 4; g++) {
    const yp    = PAD.top + (g / 4) * (pBot - PAD.top);
    const price = maxP - (g / 4) * pRange;
    ctx.strokeStyle = '#f0f0f0'; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(PAD.left, yp); ctx.lineTo(W - PAD.right, yp); ctx.stroke();
    ctx.fillStyle = '#bbb'; ctx.textAlign = 'right';
    ctx.fillText(price.toFixed(2), PAD.left - 8, yp + 4);
  }

  // Price line
  ctx.beginPath();
  data.forEach((r, i) => {
    const px = xOf(i), py = yOf(r[1]);
    i === 0 ? ctx.moveTo(px, py) : ctx.lineTo(px, py);
  });
  ctx.strokeStyle = color; ctx.lineWidth = 1.5; ctx.lineJoin = 'round';
  ctx.stroke();

  // Gradient fill
  ctx.lineTo(xOf(n - 1), pBot);
  ctx.lineTo(xOf(0), pBot);
  ctx.closePath();
  const grad = ctx.createLinearGradient(0, PAD.top, 0, pBot);
  grad.addColorStop(0, color + '35');
  grad.addColorStop(1, color + '04');
  ctx.fillStyle = grad; ctx.fill();

  // Moving average overlay
  drawMAOverlay(ctx, W, H);

  // Volume bars in the bottom band (only when showVolume is on)
  drawVolume(ctx, W, H, data, r => r[2] || 0, xOf, barW, isUp);

  // X-axis labels
  drawXLabels(ctx, data, W, H, xOf);

  // Hover crosshair — spans price + volume bands
  if (hoverIdx !== null) {
    const li = hoverIdx - viewStart;
    if (li >= 0 && li < n) {
      const hx = xOf(li), hy = yOf(data[li][1]);
      ctx.strokeStyle = '#ccc'; ctx.lineWidth = 1;
      ctx.setLineDash([4, 4]);
      ctx.beginPath(); ctx.moveTo(hx, PAD.top); ctx.lineTo(hx, H - PAD.bottom); ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = color;
      ctx.beginPath(); ctx.arc(hx, hy, 5, 0, Math.PI * 2); ctx.fill();
      ctx.fillStyle = '#fff';
      ctx.beginPath(); ctx.arc(hx, hy, 2.5, 0, Math.PI * 2); ctx.fill();
      drawTooltip(ctx, data[li], hx, W);
    }
  }
  drawMeasureOverlay(ctx, W, H);
}

function drawXLabels(ctx, data, W, H, xOf) {
  const days = new Set(data.map(r => {
    const d = new Date(r[0]);
    return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
  }));

  ctx.fillStyle = '#bbb'; ctx.textAlign = 'center'; ctx.font = '11px Arial';

  if (days.size > 1) {
    const seen = new Set();
    data.forEach((r, i) => {
      const d   = new Date(r[0]);
      const key = `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
      if (seen.has(key)) return;
      seen.add(key);
      const xp  = xOf(i);
      ctx.fillText(d.toLocaleDateString('en', { month: 'short', day: 'numeric' }), xp, H - PAD.bottom + 18);
      ctx.strokeStyle = '#e8e8e8'; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(xp, H - PAD.bottom); ctx.lineTo(xp, H - PAD.bottom + 5); ctx.stroke();
    });
  } else {
    const step = Math.max(1, Math.floor(data.length / 5));
    for (let i = 0; i < data.length; i += step) {
      const d  = new Date(data[i][0]);
      const xp = xOf(i);
      ctx.fillText(d.toLocaleTimeString('en', { hour: '2-digit', minute: '2-digit' }), xp, H - PAD.bottom + 18);
      ctx.strokeStyle = '#e8e8e8'; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(xp, H - PAD.bottom); ctx.lineTo(xp, H - PAD.bottom + 5); ctx.stroke();
    }
  }
}

function drawTooltip(ctx, row, hx, W) {
  const d   = new Date(row[0]);
  const dt  = `${d.toLocaleDateString('en', { month: 'short', day: 'numeric' })} ${d.toLocaleTimeString('en', { hour: '2-digit', minute: '2-digit' })}`;
  let tip   = `${row[1].toFixed(2)}   ${dt}`;
  if (showVolume) tip += `   Vol: ${fmtVol(row[2] || 0)}`;
  ctx.font = 'bold 11.5px Arial';
  const tw = ctx.measureText(tip).width;
  const tx = Math.min(hx + 10, W - tw - 20);
  const ty = PAD.top + 16;
  ctx.fillStyle = 'rgba(20,20,20,0.82)';
  ctx.fillRect(tx - 8, ty - 14, tw + 14, 22);
  ctx.fillStyle = '#fff'; ctx.textAlign = 'left';
  ctx.fillText(tip, tx, ty);
}

function drawMeasureOverlay(ctx, W, H) {
  if (!measureRange) return;
  const data = visData();
  if (!data.length) return;
  const n = data.length;
  const xOf = i => PAD.left + (i / Math.max(n - 1, 1)) * (W - PAD.left - PAD.right);

  const loGlobal = Math.min(measureRange.startIdx, measureRange.endIdx);
  const hiGlobal = Math.max(measureRange.startIdx, measureRange.endIdx);
  const loLocal  = Math.max(0, loGlobal - viewStart);
  const hiLocal  = Math.min(n - 1, hiGlobal - viewStart);
  if (loLocal >= hiLocal) return;

  const x1 = xOf(loLocal);
  const x2 = xOf(hiLocal);

  // Shaded selection band
  ctx.fillStyle = 'rgba(0,102,204,0.07)';
  ctx.fillRect(x1, PAD.top, x2 - x1, H - PAD.top - PAD.bottom);
  ctx.setLineDash([4, 4]);
  ctx.strokeStyle = 'rgba(0,102,204,0.35)'; ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(x1, PAD.top); ctx.lineTo(x1, H - PAD.bottom); ctx.stroke();
  ctx.beginPath(); ctx.moveTo(x2, PAD.top); ctx.lineTo(x2, H - PAD.bottom); ctx.stroke();
  ctx.setLineDash([]);

  // Change stats
  const pStart = data[loLocal][1];
  const pEnd   = data[hiLocal][1];
  const change = pEnd - pStart;
  const pct    = (change / pStart) * 100;
  const isUp   = change >= 0;
  const col    = isUp ? '#00b36b' : '#e03131';

  const fmtDate = ts => {
    const d = new Date(ts);
    return d.toLocaleDateString('en', { month: 'short', day: 'numeric' }) + ' ' +
           d.toLocaleTimeString('en', { hour: '2-digit', minute: '2-digit' });
  };
  const line1 = `${fmtDate(data[loLocal][0])}  →  ${fmtDate(data[hiLocal][0])}`;
  const line2 = `${isUp ? '+' : ''}${change.toFixed(2)}   (${isUp ? '+' : ''}${pct.toFixed(2)}%)`;

  ctx.font = '11px Arial';
  const w1 = ctx.measureText(line1).width;
  ctx.font = 'bold 13px Arial';
  const w2 = ctx.measureText(line2).width;
  const boxW = Math.max(w1, w2) + 20;
  const boxH = 46;
  const midX = (x1 + x2) / 2;
  const bx = Math.max(PAD.left, Math.min(W - PAD.right - boxW, midX - boxW / 2));
  const by = PAD.top + 4;

  ctx.fillStyle = 'rgba(20,20,20,0.88)';
  ctx.fillRect(bx, by, boxW, boxH);
  ctx.fillStyle = '#bbb'; ctx.textAlign = 'left';
  ctx.font = '11px Arial';
  ctx.fillText(line1, bx + 10, by + 16);
  ctx.fillStyle = col;
  ctx.font = 'bold 13px Arial';
  ctx.fillText(line2, bx + 10, by + 35);
}

// ── Moving Averages ───────────────────────────────────────────────────────────

function calcMA(period) {
  const result = [];
  let sum = 0;
  for (let i = 0; i < allData.length; i++) {
    sum += allData[i][1];
    if (i >= period) sum -= allData[i - period][1];
    result.push(i >= period - 1 ? sum / period : null);
  }
  return result;
}

function drawMAOverlay(ctx, W, H) {
  if (![20, 50, 200].some(p => showMA[p])) return;
  const data = visData();
  if (!data.length) return;
  const n      = data.length;
  const prices = data.map(r => r[1]);
  const minP   = Math.min(...prices);
  const maxP   = Math.max(...prices);
  const pRange = maxP - minP || 1;
  const pBot   = priceBot(H);
  const xOf    = i => PAD.left + (i / Math.max(n - 1, 1)) * (W - PAD.left - PAD.right);
  const yOf    = p => PAD.top  + (1 - (p - minP) / pRange) * (pBot - PAD.top);

  ctx.save();
  ctx.beginPath();
  ctx.rect(PAD.left, PAD.top, W - PAD.left - PAD.right, pBot - PAD.top);
  ctx.clip();

  [20, 50, 200].forEach(period => {
    if (!showMA[period]) return;
    const ma = calcMA(period);
    ctx.beginPath();
    let started = false;
    for (let i = viewStart; i <= viewEnd; i++) {
      if (ma[i] === null) continue;
      const li = i - viewStart;
      const px = xOf(li), py = yOf(ma[i]);
      if (!started) { ctx.moveTo(px, py); started = true; } else ctx.lineTo(px, py);
    }
    ctx.strokeStyle = MA_COLORS[period]; ctx.lineWidth = 1.2; ctx.lineJoin = 'round';
    ctx.stroke();
  });

  ctx.restore();

  // Labels on left y-axis
  [20, 50, 200].forEach(period => {
    if (!showMA[period]) return;
    const ma  = calcMA(period);
    const val = ma[viewEnd];
    if (val === null) return;
    const py = yOf(val);
    if (py < PAD.top || py > pBot) return;
    ctx.fillStyle = MA_COLORS[period]; ctx.textAlign = 'right'; ctx.font = 'bold 9px Arial';
    ctx.fillText('MA' + period, PAD.left - 2, py + 3);
  });
}

function toggleMA(period) {
  showMA[period] = !showMA[period];
  document.getElementById('btn-ma' + period)?.classList.toggle('active', showMA[period]);
  redraw();
}

// ── RSI Indicator ─────────────────────────────────────────────────────────────

function calcRSI(period = 14) {
  const result = new Array(allData.length).fill(null);
  if (allData.length < period + 1) return result;
  let avgGain = 0, avgLoss = 0;
  for (let i = 1; i <= period; i++) {
    const d = allData[i][1] - allData[i - 1][1];
    if (d > 0) avgGain += d; else avgLoss -= d;
  }
  avgGain /= period; avgLoss /= period;
  result[period] = avgLoss === 0 ? 100 : 100 - 100 / (1 + avgGain / avgLoss);
  for (let i = period + 1; i < allData.length; i++) {
    const d = allData[i][1] - allData[i - 1][1];
    avgGain = (avgGain * (period - 1) + Math.max(0,  d)) / period;
    avgLoss = (avgLoss * (period - 1) + Math.max(0, -d)) / period;
    result[i] = avgLoss === 0 ? 100 : 100 - 100 / (1 + avgGain / avgLoss);
  }
  return result;
}

function drawRSI() {
  const canvas = document.getElementById('rsi-chart');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  const W = canvas.width, H = canvas.height;
  const data = visData();
  if (!data.length) return;
  const n    = data.length;
  const rsi  = calcRSI();
  const vpad = { top: 8, right: PAD.right, bottom: 20, left: PAD.left };
  const xOf  = i => vpad.left + (i / Math.max(n - 1, 1)) * (W - vpad.left - vpad.right);
  const yOf  = v => vpad.top  + (1 - v / 100) * (H - vpad.top - vpad.bottom);

  ctx.clearRect(0, 0, W, H);

  // Overbought / oversold zones
  ctx.fillStyle = 'rgba(224,49,49,0.06)';
  ctx.fillRect(vpad.left, yOf(100), W - vpad.left - vpad.right, yOf(70) - yOf(100));
  ctx.fillStyle = 'rgba(0,179,107,0.06)';
  ctx.fillRect(vpad.left, yOf(30),  W - vpad.left - vpad.right, yOf(0)  - yOf(30));

  // Reference lines at 30, 50, 70
  [[70, '#e03131', [4, 4]], [50, '#e8e8e8', []], [30, '#00b36b', [4, 4]]].forEach(([level, color, dash]) => {
    const yp = yOf(level);
    ctx.strokeStyle = color; ctx.lineWidth = 1; ctx.setLineDash(dash);
    ctx.beginPath(); ctx.moveTo(vpad.left, yp); ctx.lineTo(W - vpad.right, yp); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = '#bbb'; ctx.textAlign = 'right'; ctx.font = '10px Arial';
    ctx.fillText(level, vpad.left - 4, yp + 3);
  });

  // RSI line
  ctx.beginPath();
  let started = false;
  for (let i = viewStart; i <= viewEnd; i++) {
    if (rsi[i] === null) continue;
    const li = i - viewStart;
    const px = xOf(li), py = yOf(rsi[i]);
    if (!started) { ctx.moveTo(px, py); started = true; } else ctx.lineTo(px, py);
  }
  ctx.strokeStyle = '#9b59b6'; ctx.lineWidth = 1.5; ctx.lineJoin = 'round'; ctx.stroke();

  // Label + current value
  const cur = rsi[viewEnd];
  const curCol = cur !== null ? (cur >= 70 ? '#e03131' : cur <= 30 ? '#00b36b' : '#9b59b6') : '#bbb';
  ctx.fillStyle = '#999'; ctx.textAlign = 'left'; ctx.font = 'bold 10px Arial';
  ctx.fillText('RSI(14)', vpad.left + 4, vpad.top + 10);
  if (cur !== null) {
    ctx.fillStyle = curCol;
    ctx.fillText(cur.toFixed(1), vpad.left + 60, vpad.top + 10);
  }
}

function toggleRSI() {
  showRSI = !showRSI;
  const rc  = document.getElementById('rsi-chart');
  const btn = document.getElementById('btn-rsi');
  rc.style.display = showRSI ? 'block' : 'none';
  btn?.classList.toggle('active', showRSI);
  if (showRSI) {
    rc.width  = document.getElementById('price-chart').width;
    rc.height = 80;
    drawRSI();
  }
}

// ── Volume chart ──────────────────────────────────────────────────────────────

// Overlay volume bars in the bottom band of the price-chart canvas.
// Called from drawPrice() and drawCandles() after the price area is drawn.
//   items  – array of items (price rows or candles)
//   getVol – fn(item) → volume number
//   xOf    – x-position function matching the items above (same one used to
//            place the price points / candles, so bars line up vertically)
//   barW   – width of each bar
//   isUp   – boolean used to pick the base bar colour
function drawVolume(ctx, W, H, items, getVol, xOf, barW, isUp) {
  if (!showVolume || !items.length) return;
  const vols = items.map(getVol);
  // Cap at 95th-percentile so closing-auction spikes don't crush the scale
  const sorted = [...vols].sort((a, b) => a - b);
  const maxV   = sorted[Math.floor(sorted.length * 0.95)] || Math.max(...vols) || 1;
  const color  = isUp ? '#00b36b' : '#e03131';

  const top   = priceBot(H);          // top of the volume band
  const bot   = H - PAD.bottom;       // bottom of the volume band
  const bandH = bot - top - 4;        // tiny 4px gap above bars

  // Thin separator + max-volume label on the left axis
  ctx.strokeStyle = '#e8ecf5'; ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(PAD.left, top); ctx.lineTo(W - PAD.right, top); ctx.stroke();
  ctx.fillStyle = '#8a94a6'; ctx.font = '10px Arial'; ctx.textAlign = 'right';
  ctx.fillText(fmtVol(maxV), PAD.left - 6, top + 11);

  items.forEach((item, i) => {
    const v  = getVol(item) || 0;
    const bH = Math.min(v / maxV, 1) * bandH;
    const x  = xOf(i) - barW / 2;
    const hi = hoverIdx !== null && (hoverIdx - viewStart) === i;
    ctx.fillStyle = hi ? color : color + '70';
    ctx.fillRect(x, bot - bH, barW, bH);
  });
}

// ── Candlesticks ──────────────────────────────────────────────────────────────

function toggleCandles() {
  showCandles = !showCandles;
  document.getElementById('btn-candles').classList.toggle('active', showCandles);
  document.getElementById('candle-size-select').disabled = !showCandles;
  redraw();
}

function setCandleSize() {
  const val = document.getElementById('candle-size-select').value;
  candleBucketMs = val ? parseInt(val, 10) : null;
  if (showCandles) redraw();
}

function chooseBucketMs(slice) {
  if (candleBucketMs !== null) return candleBucketMs;
  if (slice.length < 2) return 5 * 60 * 1000;
  const hours = (slice[slice.length - 1][0] - slice[0][0]) / 3_600_000;
  if (hours <= 24)  return   5 * 60 * 1000;   // 5-min candles
  if (hours <= 72)  return  30 * 60 * 1000;   // 30-min candles
  if (hours <= 168) return  60 * 60 * 1000;   // 1-hour candles
  return                  4 * 60 * 60 * 1000; // 4-hour candles
}

function aggregateOHLC(slice, bucketMs) {
  const map = new Map();
  slice.forEach(([ts, price, vol]) => {
    const key = Math.floor(ts / bucketMs) * bucketMs;
    if (!map.has(key)) {
      map.set(key, { ts: key, open: price, high: price, low: price, close: price, vol: vol || 0 });
    } else {
      const b = map.get(key);
      if (price > b.high) b.high = price;
      if (price < b.low)  b.low  = price;
      b.close = price;
      b.vol  += vol || 0;
    }
  });
  return [...map.values()].sort((a, b) => a.ts - b.ts);
}

function drawCandles() {
  const canvas = document.getElementById('price-chart');
  const ctx    = canvas.getContext('2d');
  const W = canvas.width, H = canvas.height;
  const slice = visData();
  if (slice.length < 2) return;

  const bucketMs = chooseBucketMs(slice);
  const candles  = aggregateOHLC(slice, bucketMs);
  _lastCandles   = candles;
  if (!candles.length) return;

  const n      = candles.length;
  const allP   = candles.flatMap(c => [c.high, c.low]);
  const minP   = Math.min(...allP);
  const maxP   = Math.max(...allP);
  const pRange = maxP - minP || 1;
  const slotW  = (W - PAD.left - PAD.right) / n;
  const bodyW  = Math.max(2, slotW * 0.62);
  const pBot   = priceBot(H);
  const xOf    = i => PAD.left + (i + 0.5) * slotW;
  const yOf    = p => PAD.top  + (1 - (p - minP) / pRange) * (pBot - PAD.top);

  ctx.clearRect(0, 0, W, H);
  ctx.font = '11px Arial';

  // Grid + Y labels
  for (let g = 0; g <= 4; g++) {
    const yp    = PAD.top + (g / 4) * (pBot - PAD.top);
    const price = maxP - (g / 4) * pRange;
    ctx.strokeStyle = '#f0f0f0'; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(PAD.left, yp); ctx.lineTo(W - PAD.right, yp); ctx.stroke();
    ctx.fillStyle = '#bbb'; ctx.textAlign = 'right';
    ctx.fillText(price.toFixed(2), PAD.left - 8, yp + 4);
  }

  // Find which candle the cursor is over
  let hoveredCandle = null;
  if (hoverIdx !== null) {
    const hTs = allData[hoverIdx]?.[0];
    if (hTs != null) {
      const key = Math.floor(hTs / bucketMs) * bucketMs;
      const ci  = candles.findIndex(c => c.ts === key);
      if (ci >= 0) hoveredCandle = ci;
    }
  }

  candles.forEach((c, i) => {
    const x     = xOf(i);
    const isUp  = c.close >= c.open;
    const col   = isUp ? '#00b36b' : '#e03131';
    const hcol  = isUp ? '#00d980' : '#ff3a4e';
    const color = i === hoveredCandle ? hcol : col;

    // Wick (high → low)
    ctx.strokeStyle = color; ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(x, yOf(c.high));
    ctx.lineTo(x, yOf(c.low));
    ctx.stroke();

    // Body (open ↔ close)
    const top = yOf(Math.max(c.open, c.close));
    const bot = yOf(Math.min(c.open, c.close));
    ctx.fillStyle = color;
    ctx.fillRect(x - bodyW / 2, top, bodyW, Math.max(1, bot - top));
  });

  // Moving average overlay
  drawMAOverlay(ctx, W, H);

  // Volume bars aligned under each candle (only when showVolume is on)
  const trendUp = candles[candles.length - 1].close >= candles[0].open;
  drawVolume(ctx, W, H, candles, c => c.vol, xOf, bodyW, trendUp);

  // X-axis labels (reuse existing, map candle timestamps as fake rows)
  drawXLabels(ctx, candles.map(c => [c.ts, c.close, c.vol]), W, H, xOf);

  // OHLC tooltip for hovered candle
  if (hoveredCandle !== null) {
    drawCandleTooltip(ctx, candles[hoveredCandle], xOf(hoveredCandle), W);
  }
  drawMeasureOverlay(ctx, W, H);
}

function drawCandleTooltip(ctx, c, hx, W) {
  const d   = new Date(c.ts);
  const dt  = d.toLocaleDateString('en', { month: 'short', day: 'numeric' }) +
              ' ' + d.toLocaleTimeString('en', { hour: '2-digit', minute: '2-digit' });
  const lines = [
    dt,
    `O: ${c.open.toFixed(2)}   H: ${c.high.toFixed(2)}`,
    `L: ${c.low.toFixed(2)}   C: ${c.close.toFixed(2)}`,
  ];
  if (showVolume) lines.push(`Vol: ${fmtVol(c.vol || 0)}`);
  ctx.font = 'bold 11px Arial';
  const maxW  = Math.max(...lines.map(l => ctx.measureText(l).width));
  const lineH = 16;
  const boxH  = lines.length * lineH + 10;
  const tx    = Math.min(hx + 10, W - maxW - 24);
  const ty    = PAD.top + 8;
  ctx.fillStyle = 'rgba(20,20,20,0.85)';
  ctx.fillRect(tx - 8, ty, maxW + 16, boxH);
  ctx.fillStyle = '#fff'; ctx.textAlign = 'left';
  lines.forEach((line, i) => ctx.fillText(line, tx, ty + (i + 1) * lineH));
}

// ── Interaction ───────────────────────────────────────────────────────────────

function setupEvents(canvas) {
  const idxAtX = x => {
    const rect = canvas.getBoundingClientRect();
    const frac = Math.max(0, Math.min(1, (x - rect.left - PAD.left) / (canvas.width - PAD.left - PAD.right)));
    return Math.max(viewStart, Math.min(viewEnd, Math.round(viewStart + frac * (viewEnd - viewStart))));
  };

  canvas.addEventListener('wheel', e => {
    e.preventDefault();
    const rect  = canvas.getBoundingClientRect();
    const frac  = Math.max(0, Math.min(1, (e.clientX - rect.left - PAD.left) / (canvas.width - PAD.left - PAD.right)));
    const range = viewEnd - viewStart;
    const pivot = Math.round(viewStart + frac * range);
    const newRange = Math.max(10, Math.min(allData.length - 1, Math.round(range * (e.deltaY > 0 ? 1.15 : 0.87))));
    viewStart    = Math.max(0, Math.round(pivot - frac * newRange));
    viewEnd      = Math.min(allData.length - 1, viewStart + newRange);
    measureRange = null;
    hoverIdx     = null;
    redraw();
  }, { passive: false });

  canvas.addEventListener('mousedown', e => {
    measureState = { startX: e.clientX, startIdx: idxAtX(e.clientX) };
    canvas.style.cursor = 'crosshair';
  });

  window.addEventListener('mousemove', e => {
    if (measureState) {
      const dx = Math.abs(e.clientX - measureState.startX);
      if (dx > 4) {
        const endIdx = idxAtX(e.clientX);
        measureRange = { startIdx: measureState.startIdx, endIdx };
        hoverIdx = null;
        redraw();
      }
      return;
    }
    const rect = canvas.getBoundingClientRect();
    if (e.clientX >= rect.left && e.clientX <= rect.right &&
        e.clientY >= rect.top  && e.clientY <= rect.bottom) {
      hoverIdx = idxAtX(e.clientX);
      redraw();
    }
  });

  window.addEventListener('mouseup', e => {
    if (measureState) {
      const dx = Math.abs(e.clientX - measureState.startX);
      if (dx <= 4) { measureRange = null; redraw(); }  // plain click clears measure
      measureState = null;
    }
    canvas.style.cursor = 'crosshair';
  });

  canvas.addEventListener('mouseleave', () => {
    if (!measureState) { hoverIdx = null; redraw(); }
  });
}

// ── Controls ──────────────────────────────────────────────────────────────────

function _clearRangeBtns() {
  Object.values(RANGE_BTNS).forEach(id => document.getElementById(id)?.classList.remove('active'));
}

function setRange(days) {
  // Count back `days` unique trading dates (ignores weekends/holidays with no data)
  const dayKey = ts => { const d = new Date(ts); return d.getFullYear() + '-' + d.getMonth() + '-' + d.getDate(); };
  const seen = new Set();
  let start = 0;
  for (let i = allData.length - 1; i >= 0; i--) {
    seen.add(dayKey(allData[i][0]));
    if (seen.size === days) {
      // Walk back to the first tick of this trading day
      const k = dayKey(allData[i][0]);
      start = i;
      while (start > 0 && dayKey(allData[start - 1][0]) === k) start--;
      break;
    }
  }
  viewStart    = start;
  viewEnd      = allData.length - 1;
  measureRange = null;
  _clearRangeBtns();
  document.getElementById(RANGE_BTNS[days])?.classList.add('active');
  updateTicker();
  if (_patternMode) runPatternDetection(); else redraw();
}

function resetZoom() {
  viewStart    = 0;
  viewEnd      = allData.length - 1;
  measureRange = null;
  _clearRangeBtns();
  document.getElementById('btn-7d')?.classList.add('active');
  updateTicker();
  if (_patternMode) runPatternDetection(); else redraw();
}

function toggleVolume() {
  showVolume = !showVolume;
  document.getElementById('btn-vol').classList.toggle('active', showVolume);
  redraw();
}

// ── Refresh ───────────────────────────────────────────────────────────────────

async function refreshData() {
  const btn    = document.getElementById('btn-refresh');
  const status = document.getElementById('refresh-status');
  if (btn) { btn.textContent = 'Updating…'; btn.disabled = true; }

  const relPath = STOCK.csv7d.split('/').slice(0, -1).join('/');
  try {
    await fetch('/refresh', {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: 'path=' + encodeURIComponent(relPath)
    });
    const res  = await fetch('/csv?path=' + encodeURIComponent(currentCsvPath));
    const data = await res.json();
    if (Array.isArray(data) && data.length) {
      const atEnd = viewEnd >= allData.length - 1;
      allData   = data;
      viewEnd   = atEnd ? allData.length - 1 : Math.min(viewEnd, allData.length - 1);
      viewStart = Math.min(viewStart, viewEnd);
      updateTicker();
      redraw();
      checkAlert(data[data.length - 1][1]);
      if (status) {
        const t = new Date();
        status.textContent = `Updated ${t.toLocaleTimeString('en', { hour: '2-digit', minute: '2-digit', second: '2-digit' })}`;
      }
    }
  } catch { /* ignore */ } finally {
    if (btn) { btn.textContent = 'Refresh Now'; btn.disabled = false; }
  }
}

// ── Full History ──────────────────────────────────────────────────────────────

function toggleFullHistory() {
  const btn = document.getElementById('btn-full-hist');
  usingFullHistory = !usingFullHistory;
  currentCsvPath = usingFullHistory
    ? (STOCK.csvHist || STOCK.csv7d.replace('_7d.csv', '.csv'))
    : STOCK.csv7d;
  btn.classList.toggle('active', usingFullHistory);
  btn.textContent = usingFullHistory ? '7D Only' : 'Full History';
  btn.disabled = true;
  loadCsv(currentCsvPath).catch(() => {}).finally(() => { btn.disabled = false; });
}

// ── Watchlist ─────────────────────────────────────────────────────────────────

function _stockRelPath() {
  return STOCK.csv7d.split('/').slice(0, -1).join('/');
}

async function initWatchlist() {
  try {
    const wl = await fetch('/watchlist').then(r => r.json());
    _setStarState(wl.includes(_stockRelPath()));
  } catch {}
}

async function toggleWatchlist() {
  const btn      = document.getElementById('btn-star');
  const starred  = btn.classList.contains('starred');
  const action   = starred ? 'remove' : 'add';
  try {
    const d = await fetch('/watchlist', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: _stockRelPath(), action })
    }).then(r => r.json());
    if (d.ok) _setStarState(!starred);
  } catch {}
}

function _setStarState(starred) {
  const btn = document.getElementById('btn-star');
  if (!btn) return;
  btn.innerHTML  = starred ? '&#9733;' : '&#9734;';
  btn.title      = starred ? 'Remove from watchlist' : 'Add to watchlist';
  btn.classList.toggle('starred', starred);
}

// ── Price Alerts ──────────────────────────────────────────────────────────────

async function loadAlertState() {
  try {
    const alerts = await fetch('/alerts').then(r => r.json());
    const alert  = alerts.find(a => a.path === _stockRelPath());
    _updateAlertButton(alert);
  } catch {}
}

async function saveAlert() {
  const condition = document.getElementById('alert-cond').value;
  const target    = parseFloat(document.getElementById('alert-price').value);
  if (isNaN(target) || target <= 0) return;
  closeAlertModal();
  try {
    await fetch('/alerts', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'add', path: _stockRelPath(), name: STOCK.name, condition, target })
    });
    _updateAlertButton({ condition, target });
  } catch {}
}

async function removeAlert() {
  closeAlertModal();
  try {
    await fetch('/alerts', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'remove', path: _stockRelPath() })
    });
    _updateAlertButton(null);
  } catch {}
}

function openAlertModal() {
  // Pre-fill with current alert if one exists
  fetch('/alerts').then(r => r.json()).then(alerts => {
    const a = alerts.find(a => a.path === _stockRelPath());
    if (a) {
      document.getElementById('alert-cond').value  = a.condition;
      document.getElementById('alert-price').value = a.target;
    } else {
      document.getElementById('alert-price').value = '';
    }
  }).catch(() => {});
  document.getElementById('alert-modal').style.display = '';
}

function closeAlertModal() {
  document.getElementById('alert-modal').style.display = 'none';
}

function _updateAlertButton(alert) {
  const btn = document.getElementById('btn-alert');
  if (!btn) return;
  if (alert) {
    const labels = { above: `> ${alert.target}`, below: `< ${alert.target}`,
                     pct_rise: `↑ ${alert.target}%`, pct_drop: `↓ ${alert.target}%` };
    btn.textContent = `Alert: ${labels[alert.condition] ?? alert.target}`;
    btn.classList.add('alert-set');
  } else {
    btn.textContent = 'Set Alert';
    btn.classList.remove('alert-set');
  }
}

async function checkAlert(price) {
  try {
    const alerts = await fetch('/alerts').then(r => r.json());
    const alert  = alerts.find(a => a.path === _stockRelPath());
    if (!alert) return;

    let hit = false;
    let body = '';
    if (alert.condition === 'above') {
      hit  = price >= alert.target;
      body = `${price.toFixed(2)} is above your target of ${alert.target}`;
    } else if (alert.condition === 'below') {
      hit  = price <= alert.target;
      body = `${price.toFixed(2)} is below your target of ${alert.target}`;
    } else if (alert.condition === 'pct_rise' || alert.condition === 'pct_drop') {
      const dayKey  = ts => { const d = new Date(ts); return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`; };
      const today   = allData.length ? dayKey(allData[allData.length - 1][0]) : null;
      const si      = today ? allData.findIndex(r => dayKey(r[0]) === today) : 0;
      const dayOpen = allData[si >= 0 ? si : 0]?.[1] ?? price;
      const pct     = dayOpen ? (price - dayOpen) / dayOpen * 100 : 0;
      if (alert.condition === 'pct_rise') {
        hit  = pct >=  alert.target;
        body = `Up ${pct.toFixed(2)}% intraday (target +${alert.target}%)`;
      } else {
        hit  = pct <= -alert.target;
        body = `Down ${Math.abs(pct).toFixed(2)}% intraday (target −${alert.target}%)`;
      }
    }

    if (!hit) return;
    if (Notification.permission === 'granted') {
      new Notification(`${STOCK.name} — Price Alert`, { body });
    } else if (Notification.permission !== 'denied') {
      Notification.requestPermission();
    }
  } catch {}
}

// ── Analyst Recommendations ───────────────────────────────────────────────────

async function loadRecommendations() {
  try {
    const d = await fetch('/recommendations?insref=' + encodeURIComponent(STOCK.insref)).then(r => r.json());
    if (d.error) return;
    const total = (d.buy || 0) + (d.hold || 0) + (d.sell || 0);
    if (!total && d.target_avg == null) return;

    document.getElementById('analyst-section').style.display = '';
    document.getElementById('analyst-period').textContent = d.period || '';

    if (total) {
      const buyPct  = (d.buy  / total * 100).toFixed(1);
      const holdPct = (d.hold / total * 100).toFixed(1);
      const sellPct = (d.sell / total * 100).toFixed(1);
      document.getElementById('analyst-bar-buy').style.width  = buyPct  + '%';
      document.getElementById('analyst-bar-hold').style.width = holdPct + '%';
      document.getElementById('analyst-bar-sell').style.width = sellPct + '%';
      document.getElementById('analyst-buy-n').textContent  = d.buy;
      document.getElementById('analyst-hold-n').textContent = d.hold;
      document.getElementById('analyst-sell-n').textContent = d.sell;
    }

    if (d.target_avg != null) {
      const curr  = d.currency ? ' ' + d.currency : '';
      const range = (d.target_min != null && d.target_max != null)
        ? `  <span style="color:#aaa">(${d.target_min}–${d.target_max}${curr}, ${d.target_count} analysts)</span>`
        : '';
      document.getElementById('analyst-target').innerHTML =
        `<span style="color:#888; font-size:11px; text-transform:uppercase; letter-spacing:0.5px;">Price Target</span>` +
        `&nbsp;&nbsp;<strong style="font-size:16px">${d.target_avg.toFixed(2)}${curr}</strong>${range}`;
    }
  } catch { /* recommendations unavailable — section stays hidden */ }
}

// ── News ──────────────────────────────────────────────────────────────────────

const _BADGE_CLASS = { Analyst: 'news-badge-analyst', 'Press Release': 'news-badge-press', Regulatory: 'news-badge-reg' };

async function loadNews() {
  try {
    const items = await fetch('/news?insref=' + encodeURIComponent(STOCK.insref)).then(r => r.json());
    if (items.error || !items.length) return;

    document.getElementById('news-list').innerHTML = items.map((item, i) => {
      const badgeCls = _BADGE_CLASS[item.newstype_label] || '';
      const meta = [item.date, item.time].filter(Boolean).join(' ');
      const src  = item.source ? ` &middot; ${item.source}` : '';
      return `<div class="news-item" onclick="this.classList.toggle('open')">
        <div class="news-headline">${item.headline}</div>
        <div class="news-meta">
          <span class="news-badge ${badgeCls}">${item.newstype_label}</span>${meta}${src}
        </div>
        ${item.body ? `<div class="news-body">${item.body}</div>` : ''}
      </div>`;
    }).join('');

    document.getElementById('news-section').style.display = '';
  } catch { /* news unavailable */ }
}

// ── Trades ────────────────────────────────────────────────────────────────

async function loadTrades() {
  try {
    const settings = JSON.parse(localStorage.getItem('settings') || '{}');
    const limit = Math.max(10, Math.min(50, settings.trades_limit || 25));
    const d = await fetch('/trades?insref=' + encodeURIComponent(STOCK.insref) + '&limit=' + limit).then(r => r.json());
    if (d.error || !d.trade || !d.trade.length) return;

    const dec  = d.numdec ?? 2;
    const fmtP = v => parseFloat(v).toFixed(dec);
    const fmtQ = v => fmtVol(parseFloat(v));

    document.getElementById('trades-list').innerHTML = d.trade.map((trade, i) => {
      const meta = [trade.date, trade.time].filter(Boolean).join(' ');
      return `<div style="padding:8px 0; border-bottom:1px solid #f0f0f0; display:flex; justify-content:space-between; align-items:center;">
        <div style="color:#666; font-size:11px;">${meta}</div>
        <div style="text-align:right;">
          <div style="font-weight:600; color:#1a1f36;">${fmtP(trade.tradeprice)}</div>
          <div style="color:#888; font-size:11px;">${fmtQ(trade.tradequantity)} shares</div>
        </div>
      </div>`;
    }).join('');

    document.getElementById('trades-section').style.display = '';
  } catch { /* trades unavailable */ }
}

// ── Order Book ────────────────────────────────────────────────────────────────

async function loadOrderBook() {
  const STATE_LABELS = { 0: 'Open', 1: 'Pre-open', 2: 'Closed', 3: 'Halted' };
  try {
    const d = await fetch('/orderbook?insref=' + encodeURIComponent(STOCK.insref)).then(r => r.json());
    if (d.error) return;

    const dec  = d.numdec ?? 2;
    const fmtP = v => parseFloat(v).toFixed(dec);
    const fmtQ = v => fmtVol(parseFloat(v));

    const stateEl = document.getElementById('ob-state');
    if (stateEl && d.tradestate != null) {
      stateEl.textContent = STATE_LABELS[d.tradestate] ?? '';
    }

    // Each level on its own line: price  ×  qty
    const levelHtml = (entries, color) => entries.length
      ? entries.map(e =>
          `<div style="font-size:12.5px; font-weight:600; color:${color};">
             ${fmtP(e.price)} <span style="color:#bbb; font-weight:normal; font-size:11px;">× ${fmtQ(e.quantity)}</span>
           </div>`
        ).join('')
      : '<span style="color:#ccc;">—</span>';

    document.getElementById('ob-bids').innerHTML = levelHtml(d.bid, '#00b36b');
    document.getElementById('ob-asks').innerHTML = levelHtml(d.ask, '#e03131');
  } catch { /* order book unavailable — placeholders remain */ }
}

// ── Quote / Fundamentals ──────────────────────────────────────────────────────

async function loadQuote() {
  try {
    const d = await fetch('/quote?insref=' + encodeURIComponent(STOCK.insref)).then(r => r.json());
    if (d.error) return;

    const fmtPct = v => {
      if (v == null) return null;
      const n = parseFloat(v);
      return (n >= 0 ? '+' : '') + n.toFixed(1) + '%';
    };
    const fmtNum = (v, dec = 2) => v == null ? null : parseFloat(v).toFixed(dec);
    const fmtMkCap = v => {
      if (v == null) return null;
      const n = parseFloat(v);
      if (n >= 1e12) return (n / 1e12).toFixed(2) + 'T';
      if (n >= 1e9)  return (n / 1e9).toFixed(2) + 'B';
      if (n >= 1e6)  return (n / 1e6).toFixed(2) + 'M';
      return n.toFixed(0);
    };

    // Performance returns
    const perfItems = [
      { label: '3M',  val: fmtPct(d.diff3mprc) },
      { label: 'YTD', val: fmtPct(d.diffytdprc) },
      { label: '3Y',  val: fmtPct(d.diff3yprc) },
      { label: '5Y',  val: fmtPct(d.diff5yprc) },
    ].filter(x => x.val != null);

    if (perfItems.length) {
      document.getElementById('perf-row').innerHTML = perfItems.map(({ label, val }) => {
        const color = val.startsWith('-') ? '#e03131' : '#00b36b';
        return `<div class="stat"><span class="stat-label">${label}</span><span class="stat-val" style="color:${color}">${val}</span></div>`;
      }).join('');
    }

    // Fundamentals
    const fundItems = [
      { label: 'P/E',       val: fmtNum(d.per) },
      { label: 'P/S',       val: fmtNum(d.psr) },
      { label: 'P/B',       val: fmtNum(d.pbr) },
      { label: 'EPS',       val: fmtNum(d.eps) },
      { label: 'SPS',       val: fmtNum(d.sps) },
      { label: 'DPS',       val: fmtNum(d.dps) },
      { label: 'BVPS',      val: fmtNum(d.bvps) },
      { label: 'Div Yield', val: d.dividendyield != null ? fmtNum(d.dividendyield, 2) + '%' : null },
      { label: 'Mkt Cap',   val: fmtMkCap(d.marketcap) },
    ].filter(x => x.val != null);

    if (fundItems.length) {
      document.getElementById('fund-grid').innerHTML = fundItems.map(({ label, val }) =>
        `<div class="stat"><span class="stat-label">${label}</span><span class="stat-val">${val}</span></div>`
      ).join('');
    }

    // Margins
    const marginItems = [
      { label: 'Gross Margin',     val: d.gm != null ? fmtNum(d.gm, 1) + '%' : null },
      { label: 'Operating Margin', val: d.om != null ? fmtNum(d.om, 1) + '%' : null },
      { label: 'Profit Margin',    val: d.pm != null ? fmtNum(d.pm, 1) + '%' : null },
    ].filter(x => x.val != null);

    if (marginItems.length) {
      document.getElementById('margin-row').innerHTML = marginItems.map(({ label, val }) => {
        const color = parseFloat(val) >= 0 ? '#00b36b' : '#e03131';
        return `<div class="stat"><span class="stat-label">${label}</span><span class="stat-val" style="color:${color}">${val}</span></div>`;
      }).join('');
    }

    // Company info
    const infoLines = [];
    if (d.sectorl3name) infoLines.push(`<span style="color:#888">Sector</span>&nbsp;&nbsp;${d.sectorl3name}`);
    if (d.ceo)          infoLines.push(`<span style="color:#888">CEO</span>&nbsp;&nbsp;${d.ceo}`);
    if (d.chairman)     infoLines.push(`<span style="color:#888">Chairman</span>&nbsp;&nbsp;${d.chairman}`);
    if (d.isin)         infoLines.push(`<span style="color:#888">ISIN</span>&nbsp;&nbsp;${d.isin}`);
    if (d.address)      infoLines.push(`<span style="color:#888">Address</span>&nbsp;&nbsp;${d.address}`);
    if (d.website)      infoLines.push(`<span style="color:#888">Web</span>&nbsp;&nbsp;<a href="${d.website}" target="_blank" rel="noopener" style="color:#0066cc">${d.website}</a>`);

    if (infoLines.length) {
      document.getElementById('company-info').innerHTML = infoLines.join('<br>');
    }
    if (d.description) {
      document.getElementById('company-desc').textContent = d.description;
    }

    const hasContent = perfItems.length || fundItems.length || marginItems.length || infoLines.length || d.description;
    if (hasContent) document.getElementById('quote-section').style.display = '';
  } catch { /* quote unavailable — section stays hidden */ }
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function fmtVol(v) {
  if (v >= 1e9) return (v / 1e9).toFixed(1) + 'B';
  if (v >= 1e6) return (v / 1e6).toFixed(1) + 'M';
  if (v >= 1e3) return (v / 1e3).toFixed(1) + 'K';
  return v.toFixed(0);
}

// ── Pattern Recognition ───────────────────────────────────────────────────────

let _patternMode    = false;
let _patternResults = [];
let _patternTfMs    = null;  // null = auto (same as chart display)
let _patternHorizon = 7;
const _patternFilters = { maTrend: false, rsi: false, maRegime: false };

const _HORIZON_STEPS = { 1: [1], 3: [1, 3], 7: [1, 3, 7], 14: [1, 3, 7, 14], 30: [1, 3, 7, 14, 30] };

function togglePatternMode() {
  _patternMode = !_patternMode;
  document.getElementById('btn-patterns').classList.toggle('active', _patternMode);
  document.getElementById('pattern-panel').style.display = _patternMode ? '' : 'none';
  if (_patternMode) {
    if (!showCandles) toggleCandles();   // enable candle view so markers render
    runPatternDetection();
  } else {
    redraw();
  }
}

function setPatternTf(ms) {
  _patternTfMs = ms;
  document.querySelectorAll('.pat-tf-btn').forEach(b => {
    b.classList.toggle('active', ms === null ? b.dataset.ms === 'auto' : b.dataset.ms === String(ms));
  });
  if (_patternMode) runPatternDetection();
}

function setPatternHorizon(days) {
  _patternHorizon = days;
  document.querySelectorAll('.pat-hz-btn').forEach(b => {
    b.classList.toggle('active', parseInt(b.dataset.days) === days);
  });
  if (_patternMode) _renderPatternResults();  // no re-detect needed, just re-render
}

function setPatternFilter(key) {
  _patternFilters[key] = !_patternFilters[key];
  document.querySelectorAll('.pat-flt-btn').forEach(b => {
    if (b.dataset.filter === key) b.classList.toggle('active', _patternFilters[key]);
  });
  if (_patternMode) runPatternDetection();
}

function _buildPatternContext() {
  const anyActive = Object.values(_patternFilters).some(Boolean);
  if (!anyActive) return null;

  const ma50  = calcMA(50);
  const ma200 = calcMA(200);
  const rsi   = calcRSI();
  const i     = viewEnd;

  const ma50Now  = ma50[i];
  const ma50Ago  = ma50[Math.max(0, i - 5)];
  const ma200Now = ma200[i];
  const price    = allData[i]?.[1] ?? null;

  return {
    filters:        _patternFilters,
    priceAboveMa50: ma50Now  !== null && price !== null && price > ma50Now,
    ma50SlopeUp:    ma50Now  !== null && ma50Ago !== null && ma50Now > ma50Ago,
    ma50AboveMa200: ma50Now  !== null && ma200Now !== null && ma50Now > ma200Now,
    rsiValue:       rsi[i],
  };
}

function runPatternDetection() {
  const slice    = visData();
  if (!slice.length) return;

  const bucketMs = _patternTfMs !== null ? _patternTfMs : chooseBucketMs(slice);
  const candles  = aggregateOHLC(slice, bucketMs);

  if (candles.length < 3) {
    _patternResults = [];
    _renderPatternResults();
    redraw();
    return;
  }

  const atr = PE.calcATR(candles);
  _patternResults = PE.detect(candles, atr, _buildPatternContext())
    .sort((a, b) => b.candleIdx - a.candleIdx);   // most recent first

  _renderPatternResults();
  redraw();   // redraws chart + calls drawPatternOverlay() via redraw()
}

function _renderPatternResults() {
  const el       = document.getElementById('pattern-results');
  const horizons = _HORIZON_STEPS[_patternHorizon];

  const activeFilters = Object.entries(_patternFilters).filter(([,v]) => v).map(([k]) => ({maTrend:'MA Trend', rsi:'RSI', maRegime:'MA Cross'}[k]));
  const filterBadge = activeFilters.length
    ? `<div style="font-size:11px; color:#f0a500; margin-bottom:8px;">⚡ Context filters active: ${activeFilters.join(', ')} — probabilities adjusted</div>`
    : '';

  if (!_patternResults.length) {
    el.innerHTML = filterBadge + '<div style="color:#ccc; font-size:13px; padding:8px 0;">No patterns detected in current view.</div>';
    return;
  }

  el.innerHTML = filterBadge + _patternResults.map(p => {
    const col   = p.type === 'bullish' ? '#00b36b' : p.type === 'bearish' ? '#e03131' : '#999';
    const arrow = p.type === 'bullish' ? '▲' : p.type === 'bearish' ? '▼' : '◆';
    const d     = p.candle ? new Date(p.candle.ts) : null;
    const dateStr = d
      ? d.toLocaleDateString('en', { month: 'short', day: 'numeric' }) + ' '
        + d.toLocaleTimeString('en', { hour: '2-digit', minute: '2-digit' })
      : '';

    const probs = PE.projectProbabilities(p.baseWin, horizons);
    const probHtml = probs.map(({ days, prob }) => {
      const probCol = prob >= 60 ? col : prob <= 52 ? '#ccc' : '#aaa';
      return `<span class="pat-prob">` +
             `<span class="pat-prob-day">${days}D</span> ` +
             `<span style="font-weight:600; color:${probCol};">${prob}%</span>` +
             `</span>`;
    }).join('<span style="color:#e0e0e0; margin-right:10px;">→</span>');

    return `<div class="pat-item">
      <span class="pat-arrow" style="color:${col};">${arrow}</span>
      <div style="flex:1; min-width:0;">
        <div>
          <span class="pat-name">${p.name}</span>
          <span class="pat-base">${Math.round(p.baseWin * 100)}% base</span>
          <span class="pat-date">${dateStr}</span>
        </div>
        <div class="pat-probs">${probHtml}</div>
      </div>
    </div>`;
  }).join('');
}

function drawPatternOverlay() {
  if (!_patternResults.length || !_lastCandles.length) return;
  const canvas = document.getElementById('price-chart');
  const ctx    = canvas.getContext('2d');
  const W = canvas.width, H = canvas.height;
  const candles = _lastCandles;
  const n       = candles.length;
  if (!n) return;

  // Reconstruct the same coordinate mapping as drawCandles()
  const allP   = candles.flatMap(c => [c.high, c.low]);
  const minP   = Math.min(...allP);
  const maxP   = Math.max(...allP);
  const pRange = maxP - minP || 1;
  const slotW  = (W - PAD.left - PAD.right) / n;
  const pBot   = priceBot(H);
  const xOf    = i => PAD.left + (i + 0.5) * slotW;
  const yOf    = p => PAD.top  + (1 - (p - minP) / pRange) * (pBot - PAD.top);

  // Group patterns by nearest candle in _lastCandles (handles TF mismatch)
  const byCandle = {};
  _patternResults.forEach(p => {
    const ci = candles.reduce((best, c, idx) =>
      Math.abs(c.ts - p.candle.ts) < Math.abs(candles[best].ts - p.candle.ts) ? idx : best, 0);
    if (!byCandle[ci]) byCandle[ci] = { bullish: [], bearish: [], neutral: [] };
    byCandle[ci][p.type].push(p);
  });

  ctx.save();
  Object.entries(byCandle).forEach(([ciStr, groups]) => {
    const ci = parseInt(ciStr);
    const c  = candles[ci];
    const cx = xOf(ci);

    groups.bullish.forEach((pat, k) => {
      const cy = yOf(c.low) + 12 + k * 13;
      ctx.fillStyle = '#00b36bcc';
      ctx.beginPath();
      ctx.moveTo(cx, cy - 9); ctx.lineTo(cx + 6, cy + 3); ctx.lineTo(cx - 6, cy + 3);
      ctx.closePath(); ctx.fill();
    });

    groups.bearish.forEach((pat, k) => {
      const cy = yOf(c.high) - 12 - k * 13;
      ctx.fillStyle = '#e03131cc';
      ctx.beginPath();
      ctx.moveTo(cx, cy + 9); ctx.lineTo(cx + 6, cy - 3); ctx.lineTo(cx - 6, cy - 3);
      ctx.closePath(); ctx.fill();
    });

    groups.neutral.forEach((pat, k) => {
      const cy = yOf(c.close) + (k - groups.neutral.length / 2) * 12;
      ctx.fillStyle = '#99999988';
      ctx.beginPath();
      ctx.moveTo(cx, cy - 6); ctx.lineTo(cx + 5, cy); ctx.lineTo(cx, cy + 6); ctx.lineTo(cx - 5, cy);
      ctx.closePath(); ctx.fill();
    });
  });
  ctx.restore();
}

loadData();
