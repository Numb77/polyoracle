"""
Agent 2: Mean Reversion Agent ("The Contrarian") 🔄

Fades extreme moves. If BTC has moved too far too fast (RSI extreme,
Bollinger band touch), votes for a pullback.
"""

from __future__ import annotations

import pandas as pd
import numpy as np

from agents.agent_base import BaseAgent, AgentVote, Vote
from strategy.indicators import rsi, bollinger_position, rate_of_change


class MeanReversionAgent(BaseAgent):
    """Contrarian agent — fades overextended moves."""

    @property
    def name(self) -> str:
        return "mean_reversion"

    @property
    def emoji(self) -> str:
        return "🔄"

    @property
    def persona(self) -> str:
        return "The Contrarian — fades extremes, expects mean reversion"

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

        # ── RSI extreme ───────────────────────────────────────────────────────
        if len(df_1m) >= 15:
            r = rsi(df_1m, period=14)
            if r >= 75:
                # Overbought — expect DOWN
                strength = min((r - 70) / 20, 1.0)
                signals.append(-strength)
                reasons.append(f"RSI={r:.0f} overbought")
            elif r <= 25:
                # Oversold — expect UP
                strength = min((30 - r) / 20, 1.0)
                signals.append(strength)
                reasons.append(f"RSI={r:.0f} oversold")

        # ── Bollinger Bands ───────────────────────────────────────────────────
        if len(df_1m) >= 20:
            bb_pos = bollinger_position(df_1m)
            # bb_pos: +1 = at upper band (overextended UP → expect DOWN)
            #         -1 = at lower band (overextended DOWN → expect UP)
            if abs(bb_pos) >= 0.7:
                reversion_signal = -bb_pos  # Negate: at upper → DOWN signal
                signals.append(float(reversion_signal))
                label = "upper band" if bb_pos > 0 else "lower band"
                reasons.append(f"Price at {label}")

        # ── Rate of Change ────────────────────────────────────────────────────
        # If price moved a lot very fast, expect at least a partial pullback
        if len(df_1m) >= 6:
            roc_3 = rate_of_change(df_1m, period=3)
            if abs(roc_3) >= 0.15:
                # Very large 3-candle move — bet on partial reversal
                reversion = -np.sign(roc_3) * min(abs(roc_3) / 0.3, 0.7)
                signals.append(float(reversion))
                reasons.append(f"3m RoC={roc_3:+.2f}% extreme")

        # ── Window delta fade ─────────────────────────────────────────────────
        # In the final 30 seconds, a VERY large delta sometimes gets faded
        # But only if we're at RSI/BB extremes — otherwise momentum wins
        if abs(window_delta_pct) >= 0.15 and signals:
            # Very large move — the contrarian fades it (weakly)
            fade = -np.sign(window_delta_pct) * 0.3
            signals.append(float(fade))
            reasons.append(f"Fading extreme delta={window_delta_pct:+.3f}%")

        # ── Compute conviction ────────────────────────────────────────────────
        if not signals:
            return AgentVote(
                agent_name=self.name,
                vote=Vote.ABSTAIN,
                conviction=0.0,
                reasoning="No extreme conditions detected",
            )

        net = sum(signals) / len(signals)

        if abs(net) < 0.25:
            return AgentVote(
                agent_name=self.name,
                vote=Vote.ABSTAIN,
                conviction=0.0,
                reasoning=f"Weak contrarian signal: {', '.join(reasons)}",
            )

        vote = Vote.UP if net > 0 else Vote.DOWN
        conviction = min(abs(net), 1.0)

        return AgentVote(
            agent_name=self.name,
            vote=vote,
            conviction=conviction,
            reasoning=f"Contrarian {vote.value}: {', '.join(reasons)}",
        )
