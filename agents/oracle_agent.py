"""
Agent 5: Oracle Agent ("The Arbitrageur") 🔮

Compares real-time Binance BTC price vs the last Chainlink oracle update.
If Binance shows BTC UP but Chainlink hasn't updated, there's a latency edge.
"""

from __future__ import annotations

import pandas as pd

from agents.agent_base import BaseAgent, AgentVote, Vote


class OracleAgent(BaseAgent):
    """Exploits CEX-oracle price divergence for structural edge."""

    # Oracle delta thresholds (as % divergence)
    # Raised from 0.03 → 0.05: Chainlink updates every ~5 min so small gaps
    # are common noise; only act on clearly meaningful divergence.
    SIGNIFICANT_DELTA = 0.05    # 0.05% divergence = meaningful
    STRONG_DELTA = 0.10         # 0.10% divergence = strong signal (was 0.08)

    # Maximum oracle staleness to trust
    MAX_ORACLE_LATENCY = 90.0   # seconds — beyond this, oracle signal unreliable

    @property
    def name(self) -> str:
        return "oracle"

    @property
    def emoji(self) -> str:
        return "🔮"

    @property
    def persona(self) -> str:
        return "The Arbitrageur — exploits CEX vs Chainlink oracle latency edges"

    async def vote(
        self,
        window_delta_pct: float,
        df_1m: pd.DataFrame,
        df_5s: pd.DataFrame | None,
        ob_imbalance: float | None,
        oracle_delta_pct: float,
        atr_pct: float,
        oracle_latency_sec: float = 0.0,
        **kwargs,
    ) -> AgentVote:
        """
        oracle_delta_pct: (binance_price - oracle_price) / oracle_price * 100
          Positive = Binance above oracle = oracle will likely update UP
          Negative = Binance below oracle = oracle will likely update DOWN
        """

        # If oracle data is stale or unavailable, abstain
        if oracle_latency_sec > self.MAX_ORACLE_LATENCY:
            return AgentVote(
                agent_name=self.name,
                vote=Vote.ABSTAIN,
                conviction=0.0,
                reasoning=f"Oracle stale ({oracle_latency_sec:.0f}s old)",
            )

        abs_delta = abs(oracle_delta_pct)

        # No meaningful divergence
        if abs_delta < self.SIGNIFICANT_DELTA:
            return AgentVote(
                agent_name=self.name,
                vote=Vote.ABSTAIN,
                conviction=0.0,
                reasoning=f"Oracle delta too small ({oracle_delta_pct:+.4f}%)",
            )

        # Direction: positive delta = Binance > oracle → market will resolve UP
        vote = Vote.UP if oracle_delta_pct > 0 else Vote.DOWN

        # Conviction scales with divergence
        if abs_delta >= self.STRONG_DELTA:
            conviction = min(abs_delta / 0.15, 1.0)
            note = "strong"
        else:
            conviction = abs_delta / self.SIGNIFICANT_DELTA * 0.5
            note = "moderate"

        # Confirm with window delta direction
        delta_dir = "UP" if window_delta_pct > 0 else "DOWN" if window_delta_pct < 0 else "NEUTRAL"
        if delta_dir != "NEUTRAL" and delta_dir == vote.value:
            conviction = min(conviction * 1.2, 1.0)
            alignment = " (confirms delta)"
        elif delta_dir != "NEUTRAL" and delta_dir != vote.value:
            conviction *= 0.4
            alignment = f" (conflicts delta={window_delta_pct:+.3f}%)"
        else:
            alignment = ""

        # Freshness bonus: oracle that hasn't updated in 2+ minutes has had more
        # time to accumulate divergence — the edge is more reliable, not less,
        # because it means the gap is persistent (not a transient noise spike).
        # But if oracle is very stale (>60s), that's already handled above.
        staleness_bonus = ""
        if 45 <= oracle_latency_sec <= self.MAX_ORACLE_LATENCY:
            # Scale: 45s stale → 5% boost, 90s stale → 15% boost
            stale_factor = (oracle_latency_sec - 45) / 45.0
            conviction = min(conviction * (1.0 + stale_factor * 0.15), 1.0)
            staleness_bonus = f", stale={oracle_latency_sec:.0f}s"

        return AgentVote(
            agent_name=self.name,
            vote=vote,
            conviction=min(conviction, 1.0),
            reasoning=(
                f"{note.title()} oracle edge {vote.value}: "
                f"CEX-oracle={oracle_delta_pct:+.4f}%{alignment}{staleness_bonus}"
            ),
        )
