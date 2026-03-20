"""
Build OHLCV candles from raw tick data.

Supports multiple timeframes simultaneously: 1s, 5s, 1m, 5m.
Candles are kept in a rolling buffer (deque) and exposed as pandas DataFrames.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Awaitable

import numpy as np
import pandas as pd

from core.logger import get_logger
from data.binance_ws import BtcTick

logger = get_logger(__name__)

CandleCallback = Callable[["Candle", str], Awaitable[None]]


@dataclass
class Candle:
    """A single OHLCV candle."""
    open_ts: float        # Unix timestamp of candle open
    close_ts: float       # Unix timestamp of candle close
    open: float
    high: float
    low: float
    close: float
    volume: float
    tick_count: int
    vwap: float           # Volume-weighted average price

    @property
    def is_bullish(self) -> bool:
        return self.close >= self.open

    @property
    def body_pct(self) -> float:
        """Body size as % of open price."""
        if self.open == 0:
            return 0.0
        return abs(self.close - self.open) / self.open * 100

    @property
    def range_pct(self) -> float:
        """Candle range (high - low) as % of open."""
        if self.open == 0:
            return 0.0
        return (self.high - self.low) / self.open * 100

    def to_dict(self) -> dict:
        return {
            "open_ts": self.open_ts,
            "close_ts": self.close_ts,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "tick_count": self.tick_count,
            "vwap": self.vwap,
        }


@dataclass
class _CandleAccumulator:
    """Accumulates ticks into a single candle."""
    open_ts: float
    interval_sec: int
    open: float = 0.0
    high: float = 0.0
    low: float = float("inf")
    close: float = 0.0
    volume: float = 0.0
    tick_count: int = 0
    _pv_sum: float = 0.0    # price * volume (for VWAP)

    @property
    def close_ts(self) -> float:
        return self.open_ts + self.interval_sec

    def add_tick(self, price: float, qty: float) -> None:
        if self.tick_count == 0:
            self.open = price
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.close = price
        self.volume += qty
        self._pv_sum += price * qty
        self.tick_count += 1

    def to_candle(self) -> Candle:
        vwap = self._pv_sum / self.volume if self.volume > 0 else self.close
        return Candle(
            open_ts=self.open_ts,
            close_ts=self.close_ts,
            open=self.open,
            high=self.high,
            low=self.low if self.low != float("inf") else self.open,
            close=self.close,
            volume=self.volume,
            tick_count=self.tick_count,
            vwap=vwap,
        )


# ── Timeframe definitions ─────────────────────────────────────────────────────
TIMEFRAMES = {
    "1s": 1,
    "5s": 5,
    "1m": 60,
    "5m": 300,
}

# How many candles to keep per timeframe
BUFFER_SIZES = {
    "1s": 300,    # 5 minutes of 1s candles
    "5s": 360,    # 30 minutes of 5s candles
    "1m": 60,     # 1 hour of 1m candles
    "5m": 48,     # 4 hours of 5m candles
}


class CandleBuilder:
    """
    Builds and maintains candles across multiple timeframes from tick data.

    Usage:
        builder = CandleBuilder()
        builder.on_candle_close(my_handler)
        # Feed ticks:
        await builder.on_tick(tick)
    """

    def __init__(self) -> None:
        # Active accumulator for each timeframe
        self._accumulators: dict[str, _CandleAccumulator | None] = {
            tf: None for tf in TIMEFRAMES
        }
        # Rolling candle buffers
        self._buffers: dict[str, deque[Candle]] = {
            tf: deque(maxlen=BUFFER_SIZES[tf]) for tf in TIMEFRAMES
        }
        self._callbacks: list[CandleCallback] = []
        self._tick_count: int = 0

    def on_candle_close(self, cb: CandleCallback) -> None:
        """Register a callback fired when a candle closes. Args: (candle, timeframe)."""
        self._callbacks.append(cb)

    async def on_tick(self, tick: BtcTick) -> None:
        """Feed a new BTC tick into all timeframe builders."""
        ts = tick.timestamp
        price = tick.price
        qty = tick.qty
        self._tick_count += 1

        for tf, interval in TIMEFRAMES.items():
            # Which candle bucket does this tick belong to?
            candle_open_ts = ts - (ts % interval)

            acc = self._accumulators[tf]

            # New candle period
            if acc is None or candle_open_ts > acc.open_ts:
                # Close the previous candle
                if acc is not None and acc.tick_count > 0:
                    closed_candle = acc.to_candle()
                    self._buffers[tf].append(closed_candle)
                    await self._fire(closed_candle, tf)

                # Start new accumulator
                acc = _CandleAccumulator(
                    open_ts=candle_open_ts,
                    interval_sec=interval,
                )
                self._accumulators[tf] = acc

            acc.add_tick(price, qty)

    def get_candles(self, timeframe: str) -> list[Candle]:
        """Return all closed candles for a timeframe (oldest first)."""
        if timeframe not in TIMEFRAMES:
            raise ValueError(f"Unknown timeframe: {timeframe}. Must be one of {list(TIMEFRAMES)}")
        return list(self._buffers[timeframe])

    def get_dataframe(self, timeframe: str) -> pd.DataFrame:
        """Return candles as a pandas DataFrame for use with TA libraries."""
        candles = self.get_candles(timeframe)
        if not candles:
            return pd.DataFrame(columns=["open_ts", "open", "high", "low", "close", "volume", "vwap"])

        rows = [c.to_dict() for c in candles]
        df = pd.DataFrame(rows)
        df["datetime"] = pd.to_datetime(df["open_ts"], unit="s", utc=True)
        df.set_index("datetime", inplace=True)
        return df

    def get_current_partial_candle(self, timeframe: str) -> Candle | None:
        """Return the currently-building (incomplete) candle, if any."""
        acc = self._accumulators.get(timeframe)
        if acc and acc.tick_count > 0:
            return acc.to_candle()
        return None

    def latest_close(self, timeframe: str) -> float:
        """Return the most recent closed candle's close price."""
        candles = self.get_candles(timeframe)
        if not candles:
            return 0.0
        return candles[-1].close

    def candle_count(self, timeframe: str) -> int:
        return len(self._buffers[timeframe])

    def has_enough_data(self, timeframe: str, min_candles: int) -> bool:
        return self.candle_count(timeframe) >= min_candles

    async def _fire(self, candle: Candle, timeframe: str) -> None:
        for cb in self._callbacks:
            try:
                await cb(candle, timeframe)
            except Exception as exc:
                logger.error(f"Candle callback error: {exc}", exc_info=True)
