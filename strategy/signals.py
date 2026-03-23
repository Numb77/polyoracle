"""
Signal combiner — combines all technical indicators into a weighted composite score.

Each indicator returns a score in [-1.0, +1.0].
+1.0 = strong UP signal, -1.0 = strong DOWN signal, 0.0 = neutral.

The composite score is a weighted sum normalized to [-1.0, +1.0].
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from core.logger import get_logger
from strategy.indicators import (
    ema_slope,
    ema_crossover,
    rsi_signal,
    macd_signal,
    bollinger_position,
    tick_direction_bias,
    price_momentum,
)

logger = get_logger(__name__)


@dataclass
class SignalComponent:
    """An individual signal component with its weight and score."""
    name: str
    score: float        # [-1.0, +1.0]
    weight: float       # Relative importance
    description: str = ""

    @property
    def weighted_score(self) -> float:
        return self.score * self.weight


@dataclass
class CompositeSignal:
    """The combined signal from all technical indicators."""
    components: list[SignalComponent]
    window_delta_score: float   # The primary window delta signal (weight 7)
    order_book_score: float     # Order book imbalance signal (weight 3)
    oracle_delta_score: float   # CEX vs oracle divergence (weight 2)

    @property
    def composite_score(self) -> float:
        """Weighted sum of all signals, normalized to [-1.0, +1.0]."""
        total_weight = sum(c.weight for c in self.components)
        if total_weight == 0:
            return 0.0
        weighted_sum = sum(c.weighted_score for c in self.components)
        return max(-1.0, min(1.0, weighted_sum / total_weight))

    @property
    def direction(self) -> str:
        """Predicted direction: 'UP', 'DOWN', or 'NEUTRAL'."""
        score = self.composite_score
        if score > 0.1:
            return "UP"
        elif score < -0.1:
            return "DOWN"
        return "NEUTRAL"

    @property
    def total_weight(self) -> float:
        return sum(c.weight for c in self.components)

    def to_dict(self) -> dict:
        return {
            "composite_score": round(self.composite_score, 4),
            "direction": self.direction,
            "window_delta_score": round(self.window_delta_score, 4),
            "order_book_score": round(self.order_book_score, 4),
            "oracle_delta_score": round(self.oracle_delta_score, 4),
            "components": [
                {
                    "name": c.name,
                    "score": round(c.score, 4),
                    "weight": c.weight,
                    "weighted": round(c.weighted_score, 4),
                }
                for c in self.components
            ],
        }


class SignalCombiner:
    """
    Combines all technical indicators into a composite directional signal.

    Signal weights:
    - Window Delta:       10  (PRIMARY — price vs window open, most reliable in 5-min)
    - Order Book Imbal:   4   (real-time Polymarket WS, very relevant)
    - 1-min EMA Slope:    3
    - RSI(14) on 1-min:   2
    - MACD Histogram:     2
    - Oracle Delta:       2   (from Chainlink vs Binance)
    - Bollinger Position: 1

    Total weight: 24
    """

    def __init__(self) -> None:
        self._weights = {
            "window_delta": 10,
            "ema_slope": 3,
            "rsi": 2,
            "macd": 2,
            "bollinger": 1,
            "order_book": 4,
            "oracle_delta": 2,
        }

    def compute(
        self,
        window_delta_pct: float,
        df_1m: pd.DataFrame,
        df_5s: pd.DataFrame | None = None,
        order_book_imbalance: float | None = None,
        oracle_delta_pct: float = 0.0,
    ) -> CompositeSignal:
        """
        Compute the composite signal.

        Args:
            window_delta_pct:    BTC % change from window open
            df_1m:               1-minute candle DataFrame
            df_5s:               5-second candle DataFrame (optional)
            order_book_imbalance: Polymarket YES bid/ask imbalance [-1, +1]
            oracle_delta_pct:    Chainlink vs Binance price divergence %
        """
        components = []

        # ── 1. Window Delta (weight 7) ────────────────────────────────────────
        window_score = self._score_window_delta(window_delta_pct)
        components.append(SignalComponent(
            name="window_delta",
            score=window_score,
            weight=self._weights["window_delta"],
            description=f"Window delta: {window_delta_pct:+.3f}%",
        ))

        # ── 2. EMA Slope (weight 3) ───────────────────────────────────────────
        ema_score = 0.0
        if len(df_1m) >= 10:
            slope = ema_slope(df_1m, period=8, lookback=3)
            # Normalize: slope of ±0.02% per candle → ±1.0
            ema_score = float(max(-1.0, min(1.0, slope / 0.02)))
        components.append(SignalComponent(
            name="ema_slope",
            score=ema_score,
            weight=self._weights["ema_slope"],
        ))

        # ── 3. RSI (weight 2) ─────────────────────────────────────────────────
        rsi_score = rsi_signal(df_1m) if len(df_1m) >= 15 else 0.0
        components.append(SignalComponent(
            name="rsi",
            score=rsi_score,
            weight=self._weights["rsi"],
        ))

        # ── 4. MACD (weight 2) ────────────────────────────────────────────────
        macd_score = macd_signal(df_1m) if len(df_1m) >= 35 else 0.0
        components.append(SignalComponent(
            name="macd",
            score=macd_score,
            weight=self._weights["macd"],
        ))

        # ── 5. Bollinger Bands (weight 1) ─────────────────────────────────────
        # Bollinger gives mean-reversion signal — invert for direction
        bb_pos = bollinger_position(df_1m) if len(df_1m) >= 20 else 0.0
        # If price is at upper band (+1.0), expect DOWN → negate
        bb_score = -bb_pos
        components.append(SignalComponent(
            name="bollinger",
            score=bb_score,
            weight=self._weights["bollinger"],
        ))

        # ── 6. Order Book Imbalance (weight 3) ───────────────────────────────
        ob_score = float(max(-1.0, min(1.0, order_book_imbalance))) if order_book_imbalance is not None else 0.0
        components.append(SignalComponent(
            name="order_book",
            score=ob_score,
            weight=self._weights["order_book"],
            description=f"Book imbalance: {order_book_imbalance:+.3f}" if order_book_imbalance is not None else "Book imbalance: no data",
        ))

        # ── 7. Oracle Delta (weight 2) ────────────────────────────────────────
        # If Binance shows BTC UP vs oracle, bet UP
        oracle_score = float(max(-1.0, min(1.0, oracle_delta_pct / 0.05)))
        components.append(SignalComponent(
            name="oracle_delta",
            score=oracle_score,
            weight=self._weights["oracle_delta"],
            description=f"CEX-Oracle delta: {oracle_delta_pct:+.4f}%",
        ))

        return CompositeSignal(
            components=components,
            window_delta_score=window_score,
            order_book_score=ob_score,
            oracle_delta_score=oracle_score,
        )

    def _score_window_delta(self, delta_pct: float) -> float:
        """
        Score the window delta. This is THE dominant signal.
        Direction is simply the sign of the delta.
        Magnitude is normalized.
        """
        if delta_pct == 0:
            return 0.0

        direction = 1.0 if delta_pct > 0 else -1.0
        abs_delta = abs(delta_pct)

        # Normalize: 0.10%+ → full score; scale linearly below
        magnitude = min(abs_delta / 0.10, 1.0)
        return direction * magnitude
