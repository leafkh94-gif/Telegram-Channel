"""
Pure-function technical indicators. No state, no side effects.
All functions accept plain lists or Candle sequences and return lists.
"""
from __future__ import annotations
import math
from typing import Sequence
from strategy.base import Candle


# ── New indicators (Phase 1-2 additions) ──────────────────────────────────────

def rsi(prices: Sequence[float], period: int = 14) -> list[float]:
    """Relative Strength Index. Returns index-aligned list (NaN for leading bars)."""
    n = len(prices)
    if n < period + 1:
        return [float("nan")] * n
    gains, losses = [], []
    for i in range(1, n):
        delta = prices[i] - prices[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    # Wilder seed
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    result: list[float] = [float("nan")] * period
    def _rsi(ag: float, al: float) -> float:
        return 100.0 - 100.0 / (1.0 + ag / al) if al != 0 else 100.0
    result.append(_rsi(avg_gain, avg_loss))
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        result.append(_rsi(avg_gain, avg_loss))
    return result


def macd(prices: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9
         ) -> tuple[list[float], list[float], list[float]]:
    """Returns (macd_line, signal_line, histogram) — all index-aligned."""
    fast_ema = ema(prices, fast)
    slow_ema = ema(prices, slow)
    n = len(prices)
    macd_line = [
        (f - s) if not (math.isnan(f) or math.isnan(s)) else float("nan")
        for f, s in zip(fast_ema, slow_ema)
    ]
    # Signal line is EMA of the macd_line valid portion
    first_ok = next((i for i, v in enumerate(macd_line) if not math.isnan(v)), None)
    sig_line = [float("nan")] * n
    hist = [float("nan")] * n
    if first_ok is not None:
        sub = macd_line[first_ok:]
        sub_sig = ema(sub, signal)
        for k, (m, s) in enumerate(zip(sub, sub_sig)):
            idx = first_ok + k
            sig_line[idx] = s
            if not (math.isnan(m) or math.isnan(s)):
                hist[idx] = m - s
    return macd_line, sig_line, hist


def bollinger_bands(prices: Sequence[float], period: int = 20, num_std: float = 2.0
                    ) -> tuple[list[float], list[float], list[float]]:
    """Returns (upper, mid, lower) Bollinger Bands — index-aligned."""
    n = len(prices)
    upper, mid, lower = [float("nan")] * n, [float("nan")] * n, [float("nan")] * n
    for i in range(period - 1, n):
        window = list(prices[i - period + 1: i + 1])
        m = sum(window) / period
        std = math.sqrt(sum((x - m) ** 2 for x in window) / period)
        mid[i] = m
        upper[i] = m + num_std * std
        lower[i] = m - num_std * std
    return upper, mid, lower


def bb_width(prices: Sequence[float], period: int = 20, num_std: float = 2.0) -> list[float]:
    """Bollinger Band width normalised by mid: (upper - lower) / mid."""
    upper, mid, lower = bollinger_bands(prices, period, num_std)
    result = []
    for u, m, l in zip(upper, mid, lower):
        if math.isnan(u) or math.isnan(m) or m == 0:
            result.append(float("nan"))
        else:
            result.append((u - l) / m)
    return result


def hurst_exponent(prices: Sequence[float], min_lag: int = 10, max_lag: int = 100
                   ) -> float:
    """
    Hurst exponent via R/S analysis on the last max_lag prices.
    H < 0.5 → mean-reverting, H ≈ 0.5 → random walk, H > 0.5 → trending.
    Returns nan if insufficient data.
    """
    series = list(prices[-max_lag:])
    n = len(series)
    if n < min_lag * 2:
        return float("nan")
    lags = range(min_lag, n // 2)
    rs_vals, lag_vals = [], []
    for lag in lags:
        sub = series[:lag]
        m = sum(sub) / lag
        deviations = [x - m for x in sub]
        cumdev = [sum(deviations[:i+1]) for i in range(lag)]
        r = max(cumdev) - min(cumdev)
        std = math.sqrt(sum((x - m) ** 2 for x in sub) / lag)
        if std > 0:
            rs_vals.append(math.log(r / std))
            lag_vals.append(math.log(lag))
    if len(lag_vals) < 2:
        return float("nan")
    # Linear regression slope = Hurst exponent
    n2 = len(lag_vals)
    sx = sum(lag_vals)
    sy = sum(rs_vals)
    sxy = sum(x * y for x, y in zip(lag_vals, rs_vals))
    sxx = sum(x * x for x in lag_vals)
    denom = n2 * sxx - sx * sx
    return (n2 * sxy - sx * sy) / denom if denom != 0 else float("nan")


def atr_percentile(candles: Sequence[Candle], period: int = 14, lookback: int = 100
                   ) -> float:
    """
    Current ATR as a percentile of its own recent distribution (0-100).
    Useful for dynamic volatility thresholds instead of a fixed fraction.
    """
    atr_vals = [v for v in atr(candles, period) if not math.isnan(v)]
    if not atr_vals:
        return float("nan")
    window = atr_vals[-lookback:]
    current = atr_vals[-1]
    below = sum(1 for v in window if v <= current)
    return 100.0 * below / len(window)


def ema(prices: Sequence[float], period: int) -> list[float]:
    """
    Exponential moving average. Returns a list the same length as prices;
    leading values (before the first full period) are seeded from the SMA.
    """
    if len(prices) < period:
        return []
    k = 2.0 / (period + 1)
    result: list[float] = []
    seed = sum(prices[:period]) / period
    result.append(seed)
    for p in prices[period:]:
        result.append(p * k + result[-1] * (1 - k))
    # Pad the front so indices align with the input
    return [float("nan")] * (period - 1) + result


def wilder_smooth(values: Sequence[float], period: int) -> list[float]:
    """Wilder's smoothing (used for ATR). Same index-alignment contract as ema()."""
    if len(values) < period:
        return []
    seed = sum(values[:period]) / period
    result = [seed]
    for v in values[period:]:
        result.append((result[-1] * (period - 1) + v) / period)
    return [float("nan")] * (period - 1) + result


def true_range(candles: Sequence[Candle]) -> list[float]:
    """True range for each bar (first bar has no previous close — uses high-low)."""
    tr = []
    for i, c in enumerate(candles):
        if i == 0:
            tr.append(c.high - c.low)
        else:
            prev_close = candles[i - 1].close
            tr.append(max(c.high - c.low, abs(c.high - prev_close), abs(c.low - prev_close)))
    return tr


def atr(candles: Sequence[Candle], period: int = 14) -> list[float]:
    """Average True Range using Wilder's smoothing."""
    return wilder_smooth(true_range(candles), period)


def swing_highs(candles: Sequence[Candle], lookback: int = 5) -> list[float | None]:
    """
    Returns a parallel list; index i holds the swing-high price if candle i is a
    confirmed pivot high (higher than `lookback` bars on each side), else None.
    Only indices [lookback .. len-lookback-1] can be pivots.
    """
    n = len(candles)
    result: list[float | None] = [None] * n
    for i in range(lookback, n - lookback):
        if all(candles[i].high > candles[j].high for j in range(i - lookback, i + lookback + 1) if j != i):
            result[i] = candles[i].high
    return result


def swing_lows(candles: Sequence[Candle], lookback: int = 5) -> list[float | None]:
    """Parallel list of confirmed pivot lows."""
    n = len(candles)
    result: list[float | None] = [None] * n
    for i in range(lookback, n - lookback):
        if all(candles[i].low < candles[j].low for j in range(i - lookback, i + lookback + 1) if j != i):
            result[i] = candles[i].low
    return result


def adx(candles: Sequence[Candle], period: int = 14) -> list[float]:
    """
    Average Directional Index (Wilder, 14-period default).
    Returns a same-length list; leading entries are NaN until enough bars exist.

    Interpretation:
      ADX > 25 — trending market (signals more reliable)
      ADX < 20 — choppy/ranging (suppress signals)
      20-25     — transitional
    """
    n = len(candles)
    if n < 2 * period + 1:
        return [float("nan")] * n

    # +DM, -DM, and aligned TR for bars 1..n-1
    plus_dm: list[float] = []
    minus_dm: list[float] = []
    tr_aligned: list[float] = []
    tr_vals = true_range(candles)

    for i in range(1, n):
        up   = candles[i].high - candles[i - 1].high
        down = candles[i - 1].low - candles[i].low
        plus_dm.append(up   if up > down   and up   > 0 else 0.0)
        minus_dm.append(down if down > up  and down > 0 else 0.0)
        tr_aligned.append(tr_vals[i])

    # Wilder-smooth all three (each has n-1 values)
    s_plus  = wilder_smooth(plus_dm,    period)
    s_minus = wilder_smooth(minus_dm,   period)
    s_tr    = wilder_smooth(tr_aligned, period)

    # DX for each bar in the n-1 window
    dx: list[float] = []
    for sp, sm, st in zip(s_plus, s_minus, s_tr):
        if math.isnan(sp) or math.isnan(st) or st == 0:
            dx.append(float("nan"))
        else:
            pdi   = 100.0 * sp / st
            mdi   = 100.0 * sm / st
            total = pdi + mdi
            dx.append(100.0 * abs(pdi - mdi) / total if total > 0 else 0.0)

    # Smooth DX → ADX, skipping leading NaNs
    first_ok = next((i for i, v in enumerate(dx) if not math.isnan(v)), None)
    if first_ok is None:
        return [float("nan")] * n

    adx_smoothed = wilder_smooth(dx[first_ok:], period)

    # Map back to candle-index space.
    # dx[j] belongs to candle j+1 (we skipped candle 0 when building dx).
    # adx_smoothed[k] corresponds to dx[first_ok + k] → candle first_ok + k + 1.
    result = [float("nan")] * n
    for k, val in enumerate(adx_smoothed):
        idx = first_ok + k + 1
        if idx < n:
            result[idx] = val
    return result
