"""
Market regime detection.

Classifies the current market as:
- TRENDING: Clear directional momentum — best conditions for strategy
- VOLATILE:  High volatility with momentum — good conditions
- RANGING:   Flat, choppy, no clear direction — avoid trading
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto

import numpy as np
import pandas as pd

from strategy.indicators import atr, bollinger_width, ema_slope


class Regime(Enum):
    TRENDING = auto()    # Clear trend — +15 confidence bonus
    VOLATILE = auto()    # High vol, whipsaw risk — -8 confidence penalty
    RANGING = auto()     # Flat / choppy — +0 bonus, consider skipping


@dataclass
class RegimeResult:
    regime: Regime
    bonus: float          # Confidence score adjustment (-8 to +15)
    trend_strength: float # 0.0 to 1.0
    volatility: float     # ATR as % of price
    description: str

    def to_dict(self) -> dict:
        return {
            "regime": self.regime.name,
            "bonus": self.bonus,
            "trend_strength": round(self.trend_strength, 3),
            "volatility": round(self.volatility, 4),
            "description": self.description,
        }


def detect_regime(df_1m: pd.DataFrame, df_5m: pd.DataFrame | None = None) -> RegimeResult:
    """
    Detect the current market regime from 1-minute candle data.

    Logic:
    1. Measure ATR (normalized) — high ATR = volatile
    2. Measure EMA slope — large slope = trending
    3. Measure Bollinger width — narrow = ranging
    4. Combine to classify
    """
    if len(df_1m) < 14:
        return RegimeResult(
            regime=Regime.RANGING,
            bonus=0.0,
            trend_strength=0.0,
            volatility=0.0,
            description="Insufficient data",
        )

    # Volatility: ATR as % of price
    vol = atr(df_1m, period=14)

    # Trend strength: EMA slope magnitude
    slope = abs(ema_slope(df_1m, period=8, lookback=5))

    # Bollinger width: narrow = ranging
    bb_width = bollinger_width(df_1m, period=20)

    # ── Classification logic ───────────────────────────────────────────────────
    # Trending: clear directional momentum — best conditions
    # Volatile: high ATR with some direction — good conditions
    # Ranging: truly flat — avoid or require strong SNR

    # Lowered slope threshold (0.008 → 0.005) so mild early-window trends get
    # a bonus instead of falling through to the RANGING zero-bonus bucket.
    is_trending = slope > 0.005 and bb_width > 0.04
    is_volatile = vol > 0.06
    is_ranging = vol < 0.02 and slope < 0.002

    if is_ranging:
        return RegimeResult(
            regime=Regime.RANGING,
            bonus=0.0,
            trend_strength=slope,
            volatility=vol,
            description=f"Flat/ranging market (ATR={vol:.3f}%, slope={slope:.4f})",
        )
    elif is_trending:
        trend_strength = min(1.0, slope / 0.015)
        bonus = 15.0 * trend_strength
        return RegimeResult(
            regime=Regime.TRENDING,
            bonus=bonus,
            trend_strength=trend_strength,
            volatility=vol,
            description=f"Trending market (ATR={vol:.3f}%, slope={slope:.4f})",
        )
    elif is_volatile:
        return RegimeResult(
            regime=Regime.VOLATILE,
            bonus=2.0,
            trend_strength=min(1.0, slope / 0.01),
            volatility=vol,
            description=f"Volatile/whipsaw market (ATR={vol:.3f}%) — penalised",
        )
    else:
        # Mild directional conditions — small bonus (was 3, raised to 5)
        mild_trend = min(1.0, slope / 0.005)
        return RegimeResult(
            regime=Regime.RANGING,
            bonus=5.0 * mild_trend,
            trend_strength=slope,
            volatility=vol,
            description=f"Mixed conditions (ATR={vol:.3f}%, slope={slope:.4f})",
        )
