"""
Technical indicators computed from candle data.

Uses the `ta` library for standard indicators, with custom implementations
for the window-specific metrics.

All functions accept a pandas DataFrame with OHLCV columns and return
either a float (latest value) or a Series.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

try:
    import ta
    HAS_TA = True
except ImportError:
    HAS_TA = False

from core.logger import get_logger

logger = get_logger(__name__)


def require_min_rows(df: pd.DataFrame, n: int) -> bool:
    """Check that we have enough rows for calculation."""
    return len(df) >= n


# ── EMA ───────────────────────────────────────────────────────────────────────

def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential moving average."""
    return series.ewm(span=period, adjust=False).mean()


def ema_slope(df: pd.DataFrame, period: int = 8, lookback: int = 3) -> float:
    """
    Slope of EMA over the last `lookback` candles.
    Positive = trending up, negative = trending down.
    Normalized by price to give a percentage per candle.
    """
    if not require_min_rows(df, period + lookback):
        return 0.0
    e = ema(df["close"], period)
    recent = e.iloc[-lookback:]
    if len(recent) < 2 or recent.iloc[0] == 0:
        return 0.0
    slope = (recent.iloc[-1] - recent.iloc[0]) / recent.iloc[0] * 100 / lookback
    return float(slope)


def ema_crossover(df: pd.DataFrame, fast: int = 8, slow: int = 21) -> float:
    """
    EMA crossover signal.
    Returns +1.0 if fast > slow (bullish), -1.0 if fast < slow, 0.0 at exact cross.
    """
    if not require_min_rows(df, slow + 1):
        return 0.0
    fast_ema = ema(df["close"], fast).iloc[-1]
    slow_ema = ema(df["close"], slow).iloc[-1]
    if fast_ema > slow_ema:
        return 1.0
    elif fast_ema < slow_ema:
        return -1.0
    return 0.0


# ── RSI ───────────────────────────────────────────────────────────────────────

def rsi(df: pd.DataFrame, period: int = 14) -> float:
    """
    RSI (0-100). Uses Wilder's smoothing (RMA).
    Returns NaN if insufficient data.
    """
    if not require_min_rows(df, period + 1):
        return 50.0   # Neutral when no data

    if HAS_TA:
        rsi_series = ta.momentum.RSIIndicator(df["close"], window=period).rsi()
        val = rsi_series.iloc[-1]
        return float(val) if not np.isnan(val) else 50.0

    # Fallback manual implementation
    delta = df["close"].diff()
    gains = delta.where(delta > 0, 0.0)
    losses = -delta.where(delta < 0, 0.0)
    avg_gain = gains.ewm(alpha=1/period, adjust=False).mean().iloc[-1]
    avg_loss = losses.ewm(alpha=1/period, adjust=False).mean().iloc[-1]
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return float(100 - (100 / (1 + rs)))


def rsi_signal(df: pd.DataFrame, period: int = 14) -> float:
    """
    RSI as a directional signal [-1.0, +1.0].
    Overbought (RSI>70) → -0.5 (reversal DOWN signal)
    Oversold (RSI<30) → +0.5 (reversal UP signal)
    Neutral → 0.0
    """
    r = rsi(df, period)
    if r >= 80:
        return -1.0
    elif r >= 70:
        return -0.5
    elif r <= 20:
        return 1.0
    elif r <= 30:
        return 0.5
    return 0.0


# ── MACD ──────────────────────────────────────────────────────────────────────

def macd_histogram(df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9) -> float:
    """
    MACD histogram value. Positive = bullish momentum, negative = bearish.
    Normalized by recent price for comparability.
    """
    if not require_min_rows(df, slow + signal):
        return 0.0

    if HAS_TA:
        macd_ind = ta.trend.MACD(df["close"], window_fast=fast, window_slow=slow, window_sign=signal)
        hist = macd_ind.macd_diff().iloc[-1]
        val = float(hist) if not np.isnan(hist) else 0.0
    else:
        # Manual MACD
        fast_ema = ema(df["close"], fast)
        slow_ema = ema(df["close"], slow)
        macd_line = fast_ema - slow_ema
        signal_line = ema(macd_line, signal)
        val = float((macd_line - signal_line).iloc[-1])

    # Normalize: express as % of close price
    close = df["close"].iloc[-1]
    if close == 0:
        return 0.0
    return val / close * 100


def macd_signal(df: pd.DataFrame) -> float:
    """MACD histogram as directional signal [-1.0, +1.0]."""
    hist = macd_histogram(df)
    # Clamp and normalize: typical range is ±0.1% → map to ±1.0
    return float(np.clip(hist / 0.05, -1.0, 1.0))


# ── Bollinger Bands ───────────────────────────────────────────────────────────

def bollinger_position(df: pd.DataFrame, period: int = 20, std: float = 2.0) -> float:
    """
    Position of price within Bollinger Bands.
    Returns [-1.0, +1.0]:
      +1.0 = price at upper band (overbought → DOWN signal for mean reversion)
      -1.0 = price at lower band (oversold → UP signal for mean reversion)
       0.0 = price at middle band
    """
    if not require_min_rows(df, period):
        return 0.0

    if HAS_TA:
        bb = ta.volatility.BollingerBands(df["close"], window=period, window_dev=std)
        upper = bb.bollinger_hband().iloc[-1]
        lower = bb.bollinger_lband().iloc[-1]
        middle = bb.bollinger_mavg().iloc[-1]
    else:
        middle = df["close"].rolling(period).mean().iloc[-1]
        std_dev = df["close"].rolling(period).std().iloc[-1]
        upper = middle + std * std_dev
        lower = middle - std * std_dev

    close = df["close"].iloc[-1]
    band_range = upper - lower
    if band_range == 0:
        return 0.0

    # Normalize position: 0 at lower band, 1 at upper band → map to [-1, +1]
    pos = (close - lower) / band_range  # 0 to 1
    return float(np.clip(pos * 2 - 1, -1.0, 1.0))


def bollinger_width(df: pd.DataFrame, period: int = 20, std: float = 2.0) -> float:
    """Bollinger Band width as % of middle band. Proxy for volatility."""
    if not require_min_rows(df, period):
        return 0.0

    if HAS_TA:
        bb = ta.volatility.BollingerBands(df["close"], window=period, window_dev=std)
        width = bb.bollinger_wband().iloc[-1]
        return float(width) if not np.isnan(width) else 0.0

    close_series = df["close"]
    middle = close_series.rolling(period).mean().iloc[-1]
    std_dev = close_series.rolling(period).std().iloc[-1]
    if middle == 0:
        return 0.0
    return float((std_dev * std * 2) / middle * 100)


# ── ATR ───────────────────────────────────────────────────────────────────────

def atr(df: pd.DataFrame, period: int = 14) -> float:
    """
    Average True Range — normalized by close price as %.
    Represents typical volatility.
    """
    if not require_min_rows(df, period + 1):
        return 0.0

    if HAS_TA:
        atr_val = ta.volatility.AverageTrueRange(
            df["high"], df["low"], df["close"], window=period
        ).average_true_range().iloc[-1]
    else:
        high = df["high"]
        low = df["low"]
        close = df["close"]
        prev_close = close.shift(1)
        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ], axis=1).max(axis=1)
        atr_val = tr.ewm(alpha=1/period, adjust=False).mean().iloc[-1]

    close = df["close"].iloc[-1]
    if close == 0:
        return 0.0
    return float(atr_val / close * 100)  # as % of price


# ── Rate of Change ────────────────────────────────────────────────────────────

def rate_of_change(df: pd.DataFrame, period: int = 5) -> float:
    """Price rate of change over N periods as a percentage."""
    if not require_min_rows(df, period + 1):
        return 0.0
    old_price = df["close"].iloc[-(period + 1)]
    new_price = df["close"].iloc[-1]
    if old_price == 0:
        return 0.0
    return float((new_price - old_price) / old_price * 100)


# ── Tick-level indicators (for 1s candles) ────────────────────────────────────

def tick_direction_bias(df: pd.DataFrame, lookback: int = 10) -> float:
    """
    Fraction of recent candles that are bullish (close >= open).
    Returns [-1.0, +1.0]: +1.0 = all up, -1.0 = all down.
    """
    if not require_min_rows(df, lookback):
        return 0.0
    recent = df.tail(lookback)
    bullish = (recent["close"] >= recent["open"]).sum()
    return float(bullish / lookback * 2 - 1)


def price_momentum(df: pd.DataFrame, period: int = 10) -> float:
    """
    Simple price momentum: normalized distance from recent average.
    Returns [-1.0, +1.0].
    """
    if not require_min_rows(df, period):
        return 0.0
    closes = df["close"].tail(period)
    avg = closes.mean()
    current = closes.iloc[-1]
    std = closes.std()
    if std == 0:
        return 0.0
    return float(np.clip((current - avg) / std, -1.0, 1.0))
