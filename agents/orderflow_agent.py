"""
Agent 4: Order Flow Agent ("The Book Reader") 📊

Reads the Polymarket order book. Heavy buying on YES tokens → UP signal.
Uses bid/ask imbalance ratio and depth-weighted analysis.
"""

from __future__ import annotations

import pandas as pd

from agents.agent_base import BaseAgent, AgentVote, Vote


class OrderFlowAgent(BaseAgent):
    """Reads Polymarket order book for sentiment signals."""

    STRONG_IMBALANCE = 0.35     # |imbalance| > this = strong signal
    WEAK_IMBALANCE = 0.15       # |imbalance| > this = weak signal

    @property
    def name(self) -> str:
        return "orderflow"

    @property
    def emoji(self) -> str:
        return "📊"

    @property
    def persona(self) -> str:
        return "The Book Reader — reads Polymarket order flow for smart money signals"

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
        """
        ob_imbalance: from Polymarket YES token order book
          +1.0 = all bids (smart money buying YES → UP)
          -1.0 = all asks (smart money selling YES → DOWN)
          None  = no order book data available
        """

        # If no order book data, abstain
        if ob_imbalance is None:
            return AgentVote(
                agent_name=self.name,
                vote=Vote.ABSTAIN,
                conviction=0.0,
                reasoning="No order book data available",
            )

        abs_imbalance = abs(ob_imbalance)

        if abs_imbalance < self.WEAK_IMBALANCE:
            return AgentVote(
                agent_name=self.name,
                vote=Vote.ABSTAIN,
                conviction=0.0,
                reasoning=f"Order book balanced (imbalance={ob_imbalance:+.3f})",
            )

        # Direction: positive imbalance = more bids on YES = UP
        vote = Vote.UP if ob_imbalance > 0 else Vote.DOWN

        # Conviction: scales with imbalance strength
        if abs_imbalance >= self.STRONG_IMBALANCE:
            conviction = min(abs_imbalance / 0.5, 1.0)
            strength_label = "strong"
        else:
            conviction = abs_imbalance / self.STRONG_IMBALANCE * 0.6
            strength_label = "moderate"

        # Discount if order book conflicts with window delta
        # (order book could be spoofed, but delta doesn't lie)
        delta_dir = "UP" if window_delta_pct > 0 else "DOWN" if window_delta_pct < 0 else "NEUTRAL"
        if delta_dir != "NEUTRAL" and delta_dir != vote.value:
            conviction *= 0.5
            note = f" (conflicts with delta={window_delta_pct:+.3f}%)"
        else:
            note = ""

        return AgentVote(
            agent_name=self.name,
            vote=vote,
            conviction=min(conviction, 1.0),
            reasoning=(
                f"Book {strength_label} {vote.value}: "
                f"imbalance={ob_imbalance:+.3f}{note}"
            ),
        )
