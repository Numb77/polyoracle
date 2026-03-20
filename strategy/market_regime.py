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
    VOLATILE = auto()    # High vol, some direction — +8 bonus
    RANGING = auto()     # Flat / choppy — +0 bonus, consider skipping


@dataclass
class RegimeResult:
    regime: Regime
    bonus: float          # Confidence score bonus (0-15)
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
    # Trending: high slope AND not too narrow Bollinger
    # Volatile: high ATR but no clear trend
    # Ranging: low ATR AND low slope OR very narrow Bollinger

    is_trending = slope > 0.008 and bb_width > 0.05
    is_volatile = vol > 0.08
    is_ranging = vol < 0.03 and slope < 0.003

    if is_ranging:
        return RegimeResult(
            regime=Regime.RANGING,
            bonus=0.0,
            trend_strength=slope,
            volatility=vol,
            description=f"Flat/ranging market (ATR={vol:.3f}%, slope={slope:.4f})",
        )
    elif is_trending:
        trend_strength = min(1.0, slope / 0.02)
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
            bonus=8.0,
            trend_strength=min(1.0, slope / 0.01),
            volatility=vol,
            description=f"Volatile market (ATR={vol:.3f}%)",
        )
    else:
        # Mild conditions — small bonus
        return RegimeResult(
            regime=Regime.RANGING,
            bonus=3.0,
            trend_strength=slope,
            volatility=vol,
            description=f"Mixed conditions (ATR={vol:.3f}%, slope={slope:.4f})",
        )
