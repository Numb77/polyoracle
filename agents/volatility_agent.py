"""
Agent 3: Volatility Agent ("The Risk Sentinel") 🌊

Measures volatility. In low-vol (flat) markets → ABSTAIN.
In high-vol markets → confirms momentum direction with higher conviction.
"""

from __future__ import annotations

import pandas as pd

from agents.agent_base import BaseAgent, AgentVote, Vote
from strategy.indicators import atr, bollinger_width, tick_direction_bias


class VolatilityAgent(BaseAgent):
    """Volatility sentinel — sits out flat markets, amplifies clear moves."""

    # ATR thresholds (as % of price)
    LOW_VOL_THRESHOLD = 0.03     # Below this → ABSTAIN (flat market)
    HIGH_VOL_THRESHOLD = 0.08    # Above this → high-vol regime

    @property
    def name(self) -> str:
        return "volatility"

    @property
    def emoji(self) -> str:
        return "🌊"

    @property
    def persona(self) -> str:
        return "The Risk Sentinel — avoids flat markets, confirms volatile moves"

    async def vote(
        self,
        window_delta_pct: float,
        df_1m: pd.DataFrame,
        df_5s: pd.DataFrame | None,
        ob_imbalance: float,
        oracle_delta_pct: float,
        atr_pct: float,
        **kwargs,
    ) -> AgentVote:
        """
        Logic:
        - Low volatility (ATR < 0.03%) → ABSTAIN
        - Medium volatility → vote with momentum if delta is clear
        - High volatility → vote with momentum at high conviction
        """

        # Use provided ATR or compute from data
        current_atr = atr_pct
        if current_atr == 0.0 and len(df_1m) >= 15:
            current_atr = atr(df_1m, period=14)

        # ── Low volatility: don't trade ───────────────────────────────────────
        if current_atr < self.LOW_VOL_THRESHOLD:
            return AgentVote(
                agent_name=self.name,
                vote=Vote.ABSTAIN,
                conviction=0.0,
                reasoning=f"Low volatility (ATR={current_atr:.3f}%) — staying out",
            )

        # ── Need clear directional signal ─────────────────────────────────────
        if abs(window_delta_pct) < 0.015:
            return AgentVote(
                agent_name=self.name,
                vote=Vote.ABSTAIN,
                conviction=0.0,
                reasoning=f"No clear direction (delta={window_delta_pct:+.3f}%)",
            )

        # ── Vote with momentum ────────────────────────────────────────────────
        direction = Vote.UP if window_delta_pct > 0 else Vote.DOWN

        # Conviction scales with volatility and delta size
        vol_factor = min(current_atr / self.HIGH_VOL_THRESHOLD, 1.0)
        delta_factor = min(abs(window_delta_pct) / 0.10, 1.0)

        conviction = vol_factor * 0.5 + delta_factor * 0.5

        # Tick direction bias (if 5s candles available)
        if df_5s is not None and len(df_5s) >= 10:
            bias = tick_direction_bias(df_5s, lookback=10)
            # Reduce conviction if tick direction conflicts
            if (direction == Vote.UP and bias < -0.3) or (direction == Vote.DOWN and bias > 0.3):
                conviction *= 0.6
                note = " (conflicting ticks)"
            else:
                note = ""
        else:
            note = ""

        return AgentVote(
            agent_name=self.name,
            vote=direction,
            conviction=min(conviction, 1.0),
            reasoning=(
                f"Vol agent {direction.value}: ATR={current_atr:.3f}%, "
                f"delta={window_delta_pct:+.3f}%{note}"
            ),
        )
