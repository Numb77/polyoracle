"""
Agent 1: Momentum Agent ("The Trend Rider") 🏄

Follows the dominant short-term trend. Uses EMA crossovers (8/21 on 5-second
candles) and MACD on 1-minute candles. Strongest in clear directional markets.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from agents.agent_base import BaseAgent, AgentVote, Vote
from strategy.indicators import ema_crossover, ema_slope, macd_signal, macd_histogram


class MomentumAgent(BaseAgent):
    """Follows the dominant short-term trend using EMA crossovers and MACD."""

    @property
    def name(self) -> str:
        return "momentum"

    @property
    def emoji(self) -> str:
        return "🏄"

    @property
    def persona(self) -> str:
        return "The Trend Rider — follows momentum, strongest in trending markets"

    async def vote(
        self,
        window_delta_pct: float,
        df_1m: pd.DataFrame,
        df_5s: pd.DataFrame | None,
        ob_imbalance: float | None,
        oracle_delta_pct: float,
        atr_pct: float,
        **kwargs,
    ) -> AgentVote:
        signals = []
        reasons = []

        # ── Window delta direction (primary anchor) ───────────────────────────
        if abs(window_delta_pct) >= 0.02:
            direction = 1 if window_delta_pct > 0 else -1
            strength = min(abs(window_delta_pct) / 0.10, 1.0)
            signals.append(("window_delta", direction * strength, 3.0))
            reasons.append(f"delta={window_delta_pct:+.3f}%")

        # ── EMA crossover on 5s candles ───────────────────────────────────────
        if df_5s is not None and len(df_5s) >= 25:
            cross = ema_crossover(df_5s, fast=8, slow=21)
            if cross != 0:
                signals.append(("ema_cross_5s", cross, 2.0))
                reasons.append(f"EMA8/21={'↑' if cross > 0 else '↓'}")

        # ── EMA slope on 1m candles ───────────────────────────────────────────
        if len(df_1m) >= 12:
            slope = ema_slope(df_1m, period=8, lookback=3)
            if abs(slope) > 0.003:
                normalized = np.clip(slope / 0.015, -1.0, 1.0)
                signals.append(("ema_slope_1m", float(normalized), 2.0))
                reasons.append(f"slope={'↑' if slope > 0 else '↓'}{abs(slope):.4f}")

        # ── MACD on 1m candles ────────────────────────────────────────────────
        if len(df_1m) >= 35:
            macd_hist = macd_histogram(df_1m)
            if abs(macd_hist) > 0.001:
                normalized = float(np.clip(macd_hist / 0.05, -1.0, 1.0))
                signals.append(("macd_1m", normalized, 1.5))
                reasons.append(f"MACD={'↑' if macd_hist > 0 else '↓'}")

        # ── Compute conviction ────────────────────────────────────────────────
        if not signals:
            return AgentVote(
                agent_name=self.name,
                vote=Vote.ABSTAIN,
                conviction=0.0,
                reasoning="Insufficient data",
            )

        total_weight = sum(w for _, _, w in signals)
        weighted_sum = sum(s * w for _, s, w in signals)
        net_score = weighted_sum / total_weight if total_weight > 0 else 0.0

        # Need meaningful agreement to vote
        if abs(net_score) < 0.2:
            return AgentVote(
                agent_name=self.name,
                vote=Vote.ABSTAIN,
                conviction=0.0,
                reasoning=f"Mixed signals: {', '.join(reasons)}",
            )

        vote = Vote.UP if net_score > 0 else Vote.DOWN
        conviction = min(abs(net_score), 1.0)

        return AgentVote(
            agent_name=self.name,
            vote=vote,
            conviction=conviction,
            reasoning=f"Momentum {vote.value}: {', '.join(reasons)}",
        )
