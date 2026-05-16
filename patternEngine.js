// patternEngine.js — Candlestick pattern detection engine
// Candle format (from aggregateOHLC): { ts, open, high, low, close, vol }
// Usage:
//   const atr      = PE.calcATR(candles);
//   const patterns = PE.detect(candles, atr);
//   const probs    = PE.projectProbabilities(0.63, [1, 3, 7]);

window.PE = (() => {
  'use strict';

  const DOJI_RATIO = 0.05;   // body < 5% of range → doji
  const SMALL_BODY = 0.3;    // body < 0.3×ATR → small
  const LARGE_BODY = 1.0;    // body > 1.0×ATR → large
  const LONG_WICK  = 2.0;    // wick > 2×body  → long
  const GAP_THRESH = 0.001;  // 0.1% gap threshold
  const HALF_LIFE  = 5;      // trading days for edge to halve

  // ── ATR ──────────────────────────────────────────────────────────────────────

  function calcATR(candles, period = 14) {
    if (candles.length < 2) return (candles[0]?.high - candles[0]?.low) || 1;
    const trs = [];
    for (let i = 1; i < candles.length; i++) {
      const c = candles[i], p = candles[i - 1];
      trs.push(Math.max(c.high - c.low, Math.abs(c.high - p.close), Math.abs(c.low - p.close)));
    }
    const slice = trs.slice(-period);
    return slice.reduce((a, b) => a + b, 0) / slice.length || 1;
  }

  // ── Candle properties ─────────────────────────────────────────────────────────

  function _p(c, atr) {
    const body       = Math.abs(c.close - c.open);
    const range      = c.high - c.low || 0.0001;
    const upper_wick = c.high - Math.max(c.open, c.close);
    const lower_wick = Math.min(c.open, c.close) - c.low;
    const sb         = body || 0.0001;
    return {
      body, range, upper_wick, lower_wick,
      midpoint:   (c.open + c.close) / 2,
      bull:       c.close >  c.open,
      bear:       c.close <  c.open,
      is_doji:    body < DOJI_RATIO * range,
      is_small:   body < SMALL_BODY * atr,
      is_large:   body > LARGE_BODY * atr,
      is_maru:    body > LARGE_BODY * atr && upper_wick < 0.1 * range && lower_wick < 0.1 * range,
      long_lower: lower_wick > LONG_WICK * sb,
      long_upper: upper_wick > LONG_WICK * sb,
    };
  }

  // ── Helpers ───────────────────────────────────────────────────────────────────

  function _gapUp(a, b)   { return b.low  > a.high * (1 + GAP_THRESH); }
  function _gapDown(a, b) { return b.high < a.low  * (1 - GAP_THRESH); }

  function _insideBody(outer, inner) {
    const oTop = Math.max(outer.open, outer.close);
    const oBot = Math.min(outer.open, outer.close);
    return Math.max(inner.open, inner.close) < oTop
        && Math.min(inner.open, inner.close) > oBot;
  }

  // Full candle (shadows included) fits inside outer's body
  function _insideBodyFull(outer, inner) {
    const oTop = Math.max(outer.open, outer.close);
    const oBot = Math.min(outer.open, outer.close);
    return inner.high < oTop && inner.low > oBot;
  }

  function _sameClose(a, b, atr) { return Math.abs(a.close - b.close) < 0.1 * atr; }
  function _sameOpen(a, b, atr)  { return Math.abs(a.open  - b.open)  < 0.1 * atr; }

  function _emit(out, name, type, win, i, candles) {
    out.push({ name, type, baseWin: win, candleIdx: i, candle: candles[i] });
  }

  // ── Pattern detection ─────────────────────────────────────────────────────────
  // Returns [{name, type:'bullish'|'bearish'|'neutral', baseWin, candleIdx, candle}]

  function detect(candles, atr) {
    const n   = candles.length;
    const out = [];

    for (let i = 0; i < n; i++) {
      const c0 = candles[i];
      const p0 = _p(c0, atr);

      // ── Single-candle ──

      if (p0.is_doji) {
        if      (p0.upper_wick > 2 * atr && p0.lower_wick < 0.05 * p0.range)
          _emit(out, 'Gravestone Doji',  'bearish', 0.55, i, candles);
        else if (p0.lower_wick > 2 * atr && p0.upper_wick < 0.05 * p0.range)
          _emit(out, 'Dragonfly Doji',   'bullish', 0.55, i, candles);
        else if (p0.upper_wick > atr && p0.lower_wick > atr)
          _emit(out, 'Long-Legged Doji', 'neutral', 0.50, i, candles);
        else
          _emit(out, 'Doji',             'neutral', 0.50, i, candles);
      }

      if (!p0.is_doji && p0.is_maru)
        _emit(out, p0.bull ? 'Bullish Marubozu' : 'Bearish Marubozu',
              p0.bull ? 'bullish' : 'bearish', 0.60, i, candles);

      if (!p0.is_doji && p0.long_lower && p0.upper_wick < 0.3 * (p0.body || 0.0001)) {
        const down = i >= 3 && candles[i - 3].close > c0.close;
        const up   = i >= 3 && candles[i - 3].close < c0.close;
        const sb   = p0.body || 0.0001;
        if (p0.lower_wick >= 3 * sb) {
          if (down) _emit(out, 'Takuri Line', 'bullish', 0.57, i, candles);
        } else {
          if (down) _emit(out, 'Hammer',      'bullish', 0.57, i, candles);
          if (up)   _emit(out, 'Hanging Man', 'bearish', 0.55, i, candles);
        }
      }

      if (!p0.is_doji && p0.long_upper && p0.lower_wick < 0.3 * (p0.body || 0.0001)) {
        const up   = i >= 3 && candles[i - 3].close < c0.close;
        const down = i >= 3 && candles[i - 3].close > c0.close;
        if (up)   _emit(out, 'Shooting Star',   'bearish', 0.57, i, candles);
        if (down) _emit(out, 'Inverted Hammer', 'bullish', 0.55, i, candles);
      }

      // High Wave: very long shadows on both sides, small but non-doji body
      if (!p0.is_doji && p0.is_small
          && p0.upper_wick >= 3 * (p0.body || 0.0001)
          && p0.lower_wick >= 3 * (p0.body || 0.0001))
        _emit(out, 'High Wave', 'neutral', 0.50, i, candles);

      // Belt Hold: single large candle opening at the extreme (opening marubozu)
      if (!p0.is_doji && p0.is_large) {
        const inDown = i >= 3 && candles[i - 3].close > c0.close;
        const inUp   = i >= 3 && candles[i - 3].close < c0.close;
        // Bullish Belt Hold: opens at low (no lower shadow), in downtrend
        if (p0.bull && p0.lower_wick < 0.02 * p0.range && p0.upper_wick < 0.25 * p0.range && inDown)
          _emit(out, 'Bullish Belt Hold', 'bullish', 0.56, i, candles);
        // Bearish Belt Hold: opens at high (no upper shadow), in uptrend
        if (p0.bear && p0.upper_wick < 0.02 * p0.range && p0.lower_wick < 0.25 * p0.range && inUp)
          _emit(out, 'Bearish Belt Hold', 'bearish', 0.56, i, candles);
      }

      if (i < 1) continue;
      const c1 = candles[i - 1];
      const p1 = _p(c1, atr);

      // ── Two-candle ──

      if (p1.bear && p0.bull && c0.open < c1.close && c0.close > c1.open && p0.body > p1.body)
        _emit(out, 'Bullish Engulfing', 'bullish', 0.63, i, candles);

      if (p1.bull && p0.bear && c0.open > c1.close && c0.close < c1.open && p0.body > p1.body)
        _emit(out, 'Bearish Engulfing', 'bearish', 0.63, i, candles);

      if (p1.bear && p0.bull && _gapDown(c1, c0) && c0.close > p1.midpoint
          && c0.close < Math.max(c1.open, c1.close))
        _emit(out, 'Piercing Line', 'bullish', 0.61, i, candles);

      if (p1.bull && p0.bear && _gapUp(c1, c0) && c0.close < p1.midpoint
          && c0.close > Math.min(c1.open, c1.close))
        _emit(out, 'Dark Cloud Cover', 'bearish', 0.61, i, candles);

      if (p1.bear && p1.is_large && p0.is_small && _insideBody(c1, c0)) {
        if (p0.is_doji && _insideBodyFull(c1, c0))
          _emit(out, 'Harami Cross (Bull)', 'bullish', 0.56, i, candles);
        else if (p0.bull)
          _emit(out, 'Bullish Harami', 'bullish', 0.53, i, candles);
      }

      if (p1.bull && p1.is_large && p0.is_small && _insideBody(c1, c0)) {
        if (p0.is_doji && _insideBodyFull(c1, c0))
          _emit(out, 'Harami Cross (Bear)', 'bearish', 0.56, i, candles);
        else if (p0.bear)
          _emit(out, 'Bearish Harami', 'bearish', 0.53, i, candles);
      }

      if (p1.is_maru && p0.is_maru) {
        if (p1.bear && p0.bull && _gapUp(c1, c0))   _emit(out, 'Bullish Kicker', 'bullish', 0.65, i, candles);
        if (p1.bull && p0.bear && _gapDown(c1, c0)) _emit(out, 'Bearish Kicker', 'bearish', 0.65, i, candles);
      }

      if (Math.abs(c1.low - c0.low) < 0.15 * atr && i >= 3
          && candles[i - 3].close > c0.close)
        _emit(out, 'Tweezer Bottom', 'bullish', 0.55, i, candles);

      if (Math.abs(c1.high - c0.high) < 0.15 * atr && i >= 3
          && candles[i - 3].close < c0.close)
        _emit(out, 'Tweezer Top', 'bearish', 0.55, i, candles);

      // Homing Pigeon: both bearish, C0 body inside C1 body
      if (p1.bear && p0.bear && _insideBody(c1, c0))
        _emit(out, 'Homing Pigeon', 'bullish', 0.53, i, candles);

      // Descending Hawk: both bullish, C0 body inside C1 body (bearish continuation warning)
      if (p1.bull && p0.bull && _insideBody(c1, c0))
        _emit(out, 'Descending Hawk', 'bearish', 0.53, i, candles);

      // Matching Low: both bearish marubozu, close at same level
      if (p1.bear && p1.is_maru && p0.bear && p0.is_maru && _sameClose(c1, c0, atr))
        _emit(out, 'Matching Low', 'bullish', 0.55, i, candles);

      // Matching High: both bullish marubozu, close at same level
      if (p1.bull && p1.is_maru && p0.bull && p0.is_maru && _sameClose(c1, c0, atr))
        _emit(out, 'Matching High', 'bearish', 0.55, i, candles);

      // Meeting Lines: opposite direction large candles closing at same level
      if (p1.bear && p1.is_large && p0.bull && p0.is_large && _sameClose(c1, c0, atr))
        _emit(out, 'Bullish Meeting Lines', 'bullish', 0.57, i, candles);
      if (p1.bull && p1.is_large && p0.bear && p0.is_large && _sameClose(c1, c0, atr))
        _emit(out, 'Bearish Meeting Lines', 'bearish', 0.57, i, candles);

      // Separating Lines: same open, opposite-color candles
      if (_sameOpen(c1, c0, atr)) {
        const inUp   = i >= 3 && candles[i - 3].close < c0.close;
        const inDown = i >= 3 && candles[i - 3].close > c0.close;
        if (p1.bear && p0.bull && inUp)   _emit(out, 'Bullish Separating Lines', 'bullish', 0.57, i, candles);
        if (p1.bull && p0.bear && inDown) _emit(out, 'Bearish Separating Lines', 'bearish', 0.57, i, candles);
      }

      // Doji Star: C1 large body, C0 doji with body gapping away
      if (p1.bear && p1.is_large && p0.is_doji && c0.high < Math.min(c1.open, c1.close))
        _emit(out, 'Bullish Doji Star', 'bullish', 0.55, i, candles);
      if (p1.bull && p1.is_large && p0.is_doji && c0.low > Math.max(c1.open, c1.close))
        _emit(out, 'Bearish Doji Star', 'bearish', 0.55, i, candles);

      // Tasuki Line: large candle + opposite large candle partially penetrating
      if (p1.bear && p1.is_large && p0.bull && p0.is_large
          && c0.open >= c1.close && c0.close > c1.open)
        _emit(out, 'Bullish Tasuki Line', 'bullish', 0.56, i, candles);
      if (p1.bull && p1.is_large && p0.bear && p0.is_large
          && c0.open <= c1.close && c0.close < c1.open)
        _emit(out, 'Bearish Tasuki Line', 'bearish', 0.56, i, candles);

      // Thrusting: C1 large bearish, C2 bullish opens below C1.low, closes above C1.close but below C1.midpoint
      if (p1.bear && p1.is_large && p0.bull
          && c0.open < c1.low
          && c0.close > c1.close && c0.close < p1.midpoint)
        _emit(out, 'Thrusting', 'bearish', 0.52, i, candles);

      // Windows (gaps)
      if (_gapUp(c1, c0))   _emit(out, 'Rising Window',  'bullish', 0.60, i, candles);
      if (_gapDown(c1, c0)) _emit(out, 'Falling Window', 'bearish', 0.60, i, candles);

      // Gapping Doji
      if (p0.is_doji && _gapUp(c1, c0))   _emit(out, 'Gapping Up Doji',   'bullish', 0.52, i, candles);
      if (p0.is_doji && _gapDown(c1, c0)) _emit(out, 'Gapping Down Doji', 'bearish', 0.52, i, candles);

      // Last Engulfing: engulfing pattern in a counter-trend position (trap signal)
      if (p1.bull && p0.bear && c0.open > c1.close && c0.close < c1.open) {
        const inDown = i >= 3 && candles[i - 3].close > c0.close;
        if (inDown) _emit(out, 'Last Engulfing Bottom', 'bullish', 0.54, i, candles);
      }
      if (p1.bear && p0.bull && c0.open < c1.close && c0.close > c1.open) {
        const inUp = i >= 3 && candles[i - 3].close < c0.close;
        if (inUp) _emit(out, 'Last Engulfing Top', 'bearish', 0.54, i, candles);
      }

      // Two Black Gapping Candles: both bearish, C0 gaps down from C1
      if (p1.bear && p0.bear && _gapDown(c1, c0))
        _emit(out, 'Two Black Gapping Candles', 'bearish', 0.58, i, candles);

      if (i < 2) continue;
      const c2 = candles[i - 2];
      const p2 = _p(c2, atr);

      // ── Three-candle ──

      if (p2.bear && p2.is_large && p1.is_small && p0.bull && c0.close > p2.midpoint) {
        if (p1.is_doji) _emit(out, 'Morning Doji Star', 'bullish', 0.68, i, candles);
        else            _emit(out, 'Morning Star',       'bullish', 0.65, i, candles);
      }

      if (p2.bull && p2.is_large && p1.is_small && p0.bear && c0.close < p2.midpoint) {
        if (p1.is_doji) _emit(out, 'Evening Doji Star', 'bearish', 0.68, i, candles);
        else            _emit(out, 'Evening Star',       'bearish', 0.65, i, candles);
      }

      if (p2.bull && p2.is_large && p1.bull && p1.is_large && p0.bull && p0.is_large
          && c1.open > c2.open && c1.open < c2.close
          && c0.open > c1.open && c0.open < c1.close)
        _emit(out, 'Three White Soldiers', 'bullish', 0.72, i, candles);

      if (p2.bear && p2.is_large && p1.bear && p1.is_large && p0.bear && p0.is_large
          && c1.open < c2.open && c1.open > c2.close
          && c0.open < c1.open && c0.open > c1.close)
        _emit(out, 'Three Black Crows', 'bearish', 0.72, i, candles);

      if (p2.bear && p1.bull && _insideBody(c2, c1) && p0.bull && c0.close > c1.close)
        _emit(out, 'Three Inside Up', 'bullish', 0.65, i, candles);

      if (p2.bull && p1.bear && _insideBody(c2, c1) && p0.bear && c0.close < c1.close)
        _emit(out, 'Three Inside Down', 'bearish', 0.65, i, candles);

      if (p2.bear && p1.bull && p1.body > p2.body
          && c1.open < c2.close && c1.close > c2.open
          && p0.bull && c0.close > c1.close)
        _emit(out, 'Three Outside Up', 'bullish', 0.63, i, candles);

      if (p2.bull && p1.bear && p1.body > p2.body
          && c1.open > c2.close && c1.close < c2.open
          && p0.bear && c0.close < c1.close)
        _emit(out, 'Three Outside Down', 'bearish', 0.63, i, candles);

      if (p2.is_doji && p1.is_doji && p0.is_doji) {
        if (_gapDown(c2, c1) && _gapUp(c1, c0))
          _emit(out, 'Tri-Star (Bull)', 'bullish', 0.68, i, candles);
        if (_gapUp(c2, c1) && _gapDown(c1, c0))
          _emit(out, 'Tri-Star (Bear)', 'bearish', 0.68, i, candles);
      }

      // Advance Block: 3 rising bulls with decreasing bodies and increasing upper shadows (uptrend exhaustion)
      if (p2.bull && p1.bull && p0.bull
          && c1.close > c2.close && c0.close > c1.close
          && p1.body < p2.body && p0.body < p1.body
          && p0.upper_wick > p1.upper_wick && p1.upper_wick > p2.upper_wick)
        _emit(out, 'Advance Block', 'bearish', 0.57, i, candles);

      // Deliberation: C1+C2 large bulls, C3 small bull stalling near top
      if (p2.bull && p2.is_large && p1.bull && p1.is_large
          && p0.bull && p0.is_small
          && c1.close > c2.close && c0.close >= c1.close)
        _emit(out, 'Deliberation', 'bearish', 0.55, i, candles);

      // Unique Three River Bottom
      if (p2.bear && p2.is_large && p1.bear && p1.long_lower
          && c1.low < c2.low && p0.bull && p0.is_small
          && c0.close < c1.open)
        _emit(out, 'Unique Three River Bottom', 'bullish', 0.60, i, candles);

      // Two Crows: C1 large bull, C2 bear body above C1.close, C3 bear opens in C2 body, closes in C1 body
      if (p2.bull && p2.is_large && p1.bear
          && Math.min(c1.open, c1.close) > c2.close
          && p0.bear
          && c0.open > Math.min(c1.open, c1.close) && c0.open < Math.max(c1.open, c1.close)
          && c0.close > Math.min(c2.open, c2.close) && c0.close < Math.max(c2.open, c2.close))
        _emit(out, 'Two Crows', 'bearish', 0.60, i, candles);

      // Upside Gap Two Crows: C1 bull, C2 bear gaps above C1.close, C3 bear engulfs C2 but stays above C1.close
      if (p2.bull && p1.bear && c1.low > c2.close
          && p0.bear && c0.open > c1.open && c0.close < c1.close
          && c0.close > c2.close)
        _emit(out, 'Upside Gap Two Crows', 'bearish', 0.62, i, candles);

      // Tasuki Gap (three-candle): C1 + C2 same direction with gap, C3 opposite opens inside C2 body but cannot close gap
      if (p2.bull && p1.bull && _gapUp(c2, c1) && p0.bear
          && c0.open < Math.max(c1.open, c1.close) && c0.open > Math.min(c1.open, c1.close)
          && c0.close > c2.high)
        _emit(out, 'Upside Tasuki Gap', 'bullish', 0.62, i, candles);

      if (p2.bear && p1.bear && _gapDown(c2, c1) && p0.bull
          && c0.open > Math.min(c1.open, c1.close) && c0.open < Math.max(c1.open, c1.close)
          && c0.close < c2.low)
        _emit(out, 'Downside Tasuki Gap', 'bearish', 0.62, i, candles);

      // Identical Three Crows: Three Black Crows variant where each opens at/near prior close
      if (p2.bear && p2.is_large && p1.bear && p1.is_large && p0.bear && p0.is_large
          && c1.open < c2.open && c1.open > c2.close
          && c0.open < c1.open && c0.open > c1.close
          && Math.abs(c1.open - c2.close) < 0.05 * atr
          && Math.abs(c0.open - c1.close) < 0.05 * atr)
        _emit(out, 'Identical Three Crows', 'bearish', 0.72, i, candles);

      // Side-by-Side White Lines: C1 large directional, C2+C3 bullish both gapping in same direction with similar size
      if (p2.bull && p1.bull && p0.bull && _gapUp(c2, c1) && _gapUp(c2, c0)
          && Math.abs(p1.body - p0.body) < 0.3 * atr) {
        const inUp = i >= 4 && candles[i - 4].close < c2.close;
        if (inUp) _emit(out, 'Bullish Side-by-Side White Lines', 'bullish', 0.62, i, candles);
      }
      if (p2.bear && p1.bull && p0.bull && _gapDown(c2, c1) && _gapDown(c2, c0)
          && Math.abs(p1.body - p0.body) < 0.3 * atr)
        _emit(out, 'Bearish Side-by-Side White Lines', 'bearish', 0.58, i, candles);

      // Three Stars in the South: three descending bearish candles narrowing into the low
      if (p2.bear && p2.is_large && p2.long_lower
          && p1.bear && c1.low > c2.low && c1.high < c2.high
          && p0.bear && p0.is_small
          && c0.high < c1.high && c0.low >= c1.low)
        _emit(out, 'Three Stars in the South', 'bullish', 0.63, i, candles);

      // Collapsing Doji Star: C1 bull, C2 doji gaps down, C3 bear gaps further down
      if (p2.bull && p1.is_doji && c1.high < c2.low && p0.bear && c0.high < c1.low)
        _emit(out, 'Collapsing Doji Star', 'bearish', 0.65, i, candles);
    }

    // ── Four-candle ───────────────────────────────────────────────────────────────

    for (let i = 3; i < n; i++) {
      const [a, b, c, d] = [candles[i-3], candles[i-2], candles[i-1], candles[i]];
      const [pa, pb, pc, pd] = [a, b, c, d].map(x => _p(x, atr));

      // Concealing Baby Swallow: C1+C2 Black Marubozu, C3 bearish with upper shadow into C2 and no lower shadow,
      //   C4 large bearish engulfs C3 including its shadows
      if (pa.bear && pa.is_maru && pb.bear && pb.is_maru
          && pc.bear && c.high > b.close && c.low >= b.open  // upper shadow above C2.close, no lower shadow (gap body)
          && pd.bear && pd.is_large && d.open > c.high && d.close < c.low)
        _emit(out, 'Concealing Baby Swallow', 'bullish', 0.62, i, candles);
    }

    // ── Five-candle: Rising / Falling Three Methods + Ladder + Breakaway ─────────

    for (let i = 4; i < n; i++) {
      const [a, b, c, d, e] = [candles[i-4], candles[i-3], candles[i-2], candles[i-1], candles[i]];
      const [pa, pb, pc, pd, pe] = [a, b, c, d, e].map(x => _p(x, atr));

      if (pa.bull && pa.is_large
          && pb.bear && !pb.is_large && b.low > a.low && b.high < a.high
          && pc.bear && !pc.is_large && c.low > a.low && c.high < a.high
          && pd.bear && !pd.is_large && d.low > a.low && d.high < a.high
          && pe.bull && pe.is_large  && e.close > a.close)
        _emit(out, 'Rising Three Methods', 'bullish', 0.70, i, candles);

      if (pa.bear && pa.is_large
          && pb.bull && !pb.is_large && b.high < a.high && b.low > a.low
          && pc.bull && !pc.is_large && c.high < a.high && c.low > a.low
          && pd.bull && !pd.is_large && d.high < a.high && d.low > a.low
          && pe.bear && pe.is_large  && e.close < a.close)
        _emit(out, 'Falling Three Methods', 'bearish', 0.70, i, candles);

      // Ladder Bottom: C1-C3 bearish with descending opens, C4 bearish with upper shadow in C3 body, C5 bullish gap up
      if (pa.bear && pb.bear && pc.bear
          && b.open < a.open && c.open < b.open
          && pd.bear && d.high > c.close && d.high < Math.max(c.open, c.close)
          && pe.bull && e.open > d.open)
        _emit(out, 'Ladder Bottom', 'bullish', 0.65, i, candles);

      // Ladder Top: C1-C3 bullish with ascending opens, C4 bullish with lower shadow in C3 body, C5 bearish gap down
      if (pa.bull && pb.bull && pc.bull
          && b.open > a.open && c.open > b.open
          && pd.bull && d.low < c.close && d.low > Math.min(c.open, c.close)
          && pe.bear && e.open < d.open)
        _emit(out, 'Ladder Top', 'bearish', 0.65, i, candles);

      // Bullish Breakaway: C1 large bearish, C2 bearish gaps down, C3+C4 bearish declining, C5 large bullish closes above C2.close
      if (pa.bear && pa.is_large
          && pb.bear && _gapDown(a, b)
          && pc.bear && pd.bear && d.close < c.close
          && pe.bull && pe.is_large && e.close > b.close)
        _emit(out, 'Bullish Breakaway', 'bullish', 0.65, i, candles);

      // Bearish Breakaway: C1 large bullish, C2 bullish gaps up, C3+C4 bullish ascending, C5 large bearish closes below C2.open
      if (pa.bull && pa.is_large
          && pb.bull && _gapUp(a, b)
          && pc.bull && pd.bull && d.close > c.close
          && pe.bear && pe.is_large && e.close < b.open && e.close > a.close)
        _emit(out, 'Bearish Breakaway', 'bearish', 0.65, i, candles);
    }

    return out;
  }

  // ── Probability projection ────────────────────────────────────────────────────
  // P(t) = 0.5 + (base − 0.5) × exp(−t × ln2 / HALF_LIFE)

  function projectProbabilities(baseWin, horizons) {
    const edge = baseWin - 0.5;
    const k    = Math.LN2 / HALF_LIFE;
    return horizons.map(t => ({
      days: t,
      prob: Math.round((0.5 + edge * Math.exp(-k * t)) * 100),
    }));
  }

  // ── Intraday helpers ──────────────────────────────────────────────────────────

  // Returns true if the proposed order value is large enough to avoid the
  // minimum-commission trap. At 0.06% commission, the minimum kicks in below
  // 20 / 0.0006 = 33,333 SEK. Below that threshold the round-trip costs 40 SEK
  // minimum regardless of profit, requiring >0.36% just to break even on fees.
  function isCommissionViable(orderValueSEK, commissionRate = 0.0006, minCommission = 20) {
    return orderValueSEK >= (minCommission / commissionRate);
  }

  // Returns a score in [0, 1] representing how close `price` is to the session
  // high. Score 1.0 = at the high; score 0.0 = at or below session low.
  // Values above 0.8 (within ~0.2% of high) indicate poor long-entry timing.
  function sessionHighProximity(price, sessionHigh, sessionLow) {
    const range = sessionHigh - sessionLow;
    if (range <= 0) return 1;
    return Math.max(0, Math.min(1, (price - sessionLow) / range));
  }

  return { detect, calcATR, projectProbabilities, isCommissionViable, sessionHighProximity };
})();
