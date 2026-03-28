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

    # ATR thresholds (as % of price) — aligned with market_regime.py thresholds
    LOW_VOL_THRESHOLD = 0.02     # Below this → SNR-based check (truly flat)
    HIGH_VOL_THRESHOLD = 0.06    # Above this → high-vol regime

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
        ob_imbalance: float | None,
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

        # ── No ATR data yet (early window, < 14 candles) ─────────────────────
        # Fall back to raw delta magnitude so the agent doesn't blind-abstain
        # during the first 2-3 minutes when ATR is unavailable.
        if current_atr == 0.0:
            if abs(window_delta_pct) >= 0.05:
                direction = Vote.UP if window_delta_pct > 0 else Vote.DOWN
                # Conviction scales with delta; 0.20%+ = max conviction
                conviction = min(abs(window_delta_pct) / 0.20, 0.85)
                return AgentVote(
                    agent_name=self.name,
                    vote=direction,
                    conviction=conviction,
                    reasoning=(
                        f"No ATR yet — delta-only {direction.value}: "
                        f"delta={window_delta_pct:+.3f}%"
                    ),
                )
            return AgentVote(
                agent_name=self.name,
                vote=Vote.ABSTAIN,
                conviction=0.0,
                reasoning=f"No ATR data and delta too small ({window_delta_pct:+.3f}%)",
            )

        # ── Signal-to-noise ratio: delta relative to current volatility ──────
        # A small delta in a dead-flat market can be more meaningful than
        # a large delta in a noisy, high-vol market.
        snr = abs(window_delta_pct) / current_atr if current_atr > 0 else 0.0

        # ── Low volatility: use SNR instead of blanket ABSTAIN ────────────────
        if current_atr < self.LOW_VOL_THRESHOLD:
            if snr < 1.5:
                # Delta doesn't stand out above the noise floor — skip
                return AgentVote(
                    agent_name=self.name,
                    vote=Vote.ABSTAIN,
                    conviction=0.0,
                    reasoning=(
                        f"Low vol, weak signal (ATR={current_atr:.3f}%, "
                        f"delta={window_delta_pct:+.3f}%, SNR={snr:.2f})"
                    ),
                )
            # Clear signal in a flat market — SNR drives conviction
            # SNR 1.5 → 0.30 conviction, SNR 5.0+ → 0.75 (capped)
            direction = Vote.UP if window_delta_pct > 0 else Vote.DOWN
            conviction = min(snr / 5.0, 0.75)
            if df_5s is not None and len(df_5s) >= 10:
                bias = tick_direction_bias(df_5s, lookback=10)
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
                conviction=conviction,
                reasoning=(
                    f"Low-vol breakout {direction.value}: ATR={current_atr:.3f}%, "
                    f"delta={window_delta_pct:+.3f}%, SNR={snr:.2f}{note}"
                ),
            )

        # ── Normal/high vol: need a clear directional signal ──────────────────
        if abs(window_delta_pct) < 0.01:
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
