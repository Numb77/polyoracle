"""
Confidence score engine.

Takes the composite signal + agent consensus + market context
and produces a single 0-100 confidence score.

Score ≥ MIN_CONFIDENCE_SCORE → place trade.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.config import get_config
from core.logger import get_logger
from strategy.signals import CompositeSignal

logger = get_logger(__name__)
cfg = get_config()


@dataclass
class ConfidenceBreakdown:
    """Detailed breakdown of how the confidence score was computed."""
    signal_contribution: float      # 0-40 (from composite score magnitude)
    agent_contribution: float       # 0-25 (from agent consensus)
    delta_contribution: float       # 0-25 (from window delta decisiveness)
    regime_contribution: float      # 0-15 (from market regime)
    total: float                    # 0-100
    momentum_contribution: float = 0.0          # 0-5   (accelerating delta bonus)
    time_decay_contribution: float = 0.0        # 0-10  (PM odds near deadline)
    persistence_contribution: float = 0.0       # 0-12  (sustained move over time)
    cross_asset_contribution: float = 0.0       # -8..+5 (BTC/ETH alignment)

    @property
    def should_trade(self) -> bool:
        return self.total >= cfg.min_confidence_score

    def to_dict(self) -> dict:
        return {
            "signal_contribution": round(self.signal_contribution, 1),
            "agent_contribution": round(self.agent_contribution, 1),
            "delta_contribution": round(self.delta_contribution, 1),
            "regime_contribution": round(self.regime_contribution, 1),
            "momentum_contribution": round(self.momentum_contribution, 1),
            "time_decay_contribution": round(self.time_decay_contribution, 1),
            "persistence_contribution": round(self.persistence_contribution, 1),
            "cross_asset_contribution": round(self.cross_asset_contribution, 1),
            "total": round(self.total, 1),
            "should_trade": self.should_trade,
        }


class ConfidenceEngine:
    """
    Computes a 0-100 confidence score based on:
    1. Signal magnitude (0-40 pts)
    2. Agent consensus (0-25 pts)
    3. Window delta decisiveness (0-20 pts)
    4. Market regime (0-15 pts)
    5. Momentum acceleration bonus (0-5 pts)
    """

    def compute(
        self,
        composite_signal: CompositeSignal,
        window_delta_pct: float,
        agent_agreement_ratio: float,   # 0.0 to 1.0
        regime_bonus: float,            # 0 to 15
        delta_acceleration: float = 0.0,  # delta change since last eval (same sign = accelerating)
        regime_volatility: float = 0.0,   # ATR as % of price from regime detector
        remaining_sec: float = 300.0,     # Seconds until window closes
        polymarket_alignment: float = 0.0,  # [-1,+1]: PM odds aligned with our direction
        elapsed_sec: float = 0.0,          # Seconds since window opened
        cross_asset_alignment: float = 0.0, # [-1,+1]: other asset moving same direction
    ) -> ConfidenceBreakdown:
        """
        Compute confidence score.

        Args:
            composite_signal:       Combined technical signal
            window_delta_pct:       BTC % change from window open
            agent_agreement_ratio:  Fraction of agents that agree (0-1)
            regime_bonus:           Bonus points from market regime (0-15)
            remaining_sec:          Seconds until window closes (for time-decay bonus)
            polymarket_alignment:   How strongly PM odds confirm our direction [-1,+1]
        """
        # ── 1. Signal magnitude (0-40 pts) ───────────────────────────────────
        abs_score = abs(composite_signal.composite_score)
        signal_pts = min(abs_score * 40, 40.0)

        # ── 2. Agent consensus (0-25 pts) ────────────────────────────────────
        # agreement_ratio = n_agreeing / n_total_agents (denominator = 5).
        # Tiers are calibrated to this denominator so that:
        #   1/5 (solo)  → 2 pts  (minimal — gate blocks solo trades anyway)
        #   2/5 (two agree) → 7-14 pts  (meaningful early-window signal)
        #   3/5 (three) → 14-20 pts (strong)
        #   4/5 (four)  → 20-24 pts
        #   5/5 (unanimous) → 25 pts
        if agent_agreement_ratio >= 1.0:
            agent_pts = 25.0
        elif agent_agreement_ratio >= 0.8:
            agent_pts = 20.0 + (agent_agreement_ratio - 0.8) / 0.2 * 5.0
        elif agent_agreement_ratio >= 0.6:
            agent_pts = 14.0 + (agent_agreement_ratio - 0.6) / 0.2 * 6.0
        elif agent_agreement_ratio >= 0.4:
            # 2/5 agents agree → 7 pts; 2.5/5 → ~10 pts
            agent_pts = 7.0 + (agent_agreement_ratio - 0.4) / 0.2 * 7.0
        elif agent_agreement_ratio >= 0.2:
            # 1/5 solo → 2 pts (minimum signal; minimum-vote gate still applies)
            agent_pts = 2.0 + (agent_agreement_ratio - 0.2) / 0.2 * 5.0
        else:
            agent_pts = 0.0

        # ── 3. Window delta decisiveness (0-25 pts) ──────────────────────────
        # Raised from 20 → 25 max (delta is the primary signal for 5-min windows).
        # Linear interpolation within bands to avoid point cliffs.
        abs_delta = abs(window_delta_pct)
        if abs_delta >= 0.10:
            delta_pts = 25.0
        elif abs_delta >= 0.05:
            # 12 → 25 pts linearly across 0.05–0.10%
            delta_pts = 12.0 + (abs_delta - 0.05) / 0.05 * 13.0
        elif abs_delta >= 0.02:
            # 4 → 12 pts linearly across 0.02–0.05%
            delta_pts = 4.0 + (abs_delta - 0.02) / 0.03 * 8.0
        else:
            # 0 → 4 pts linearly across 0–0.02%
            delta_pts = abs_delta / 0.02 * 4.0

        # ── Vol-adjusted delta scaling ────────────────────────────────────────
        # In a low-vol (flat) market, the same absolute delta is a stronger
        # signal because there's less noise drowning it out.
        # Reference: 0.05% ATR is "normal" — below this, delta scores higher.
        # Scale is capped at 2.0× to avoid over-weighting micro-moves.
        if regime_volatility > 0:
            vol_scale = min(0.05 / regime_volatility, 2.0)
            vol_scale = max(vol_scale, 1.0)   # only boost, never penalise
            delta_pts = min(delta_pts * vol_scale, 25.0)

        # ── 4. Market regime bonus (0-15 pts) ────────────────────────────────
        regime_pts = float(max(0.0, min(15.0, regime_bonus)))

        # ── 5. Momentum acceleration bonus (0-5 pts) ─────────────────────────
        # If delta is growing in the same direction since last evaluation,
        # the move is accelerating — higher certainty for late-window resolution.
        # delta_acceleration = current_delta - prev_delta (same sign = accelerating).
        momentum_pts = 0.0
        same_direction = (window_delta_pct * delta_acceleration) > 0
        if same_direction and abs(delta_acceleration) >= 0.015:
            # Acceleration of 0.015% → 2.5 pts, 0.03%+ → 5 pts (capped)
            momentum_pts = min(5.0, abs(delta_acceleration) / 0.03 * 5.0)

        # ── 6. Time-decay bonus (0-10 pts) ───────────────────────────────────
        # As the window deadline approaches, prediction market odds become more
        # informative: there is less time for the outcome to reverse, so a
        # strong PM probability in our direction is a high-quality confirming
        # signal.  We give up to 10 extra points when:
        #   (a) fewer than 90s remain (the deadline zone), AND
        #   (b) Polymarket odds clearly lean in our direction.
        # The bonus grows linearly from 0 at 90s to full at 0s remaining,
        # scaled by how strongly PM aligns with our trade direction.
        time_decay_pts = 0.0
        if remaining_sec <= 90.0 and polymarket_alignment > 0.05:
            # time_pressure: 0 at 90s remaining, 1 at 0s remaining
            time_pressure = max(0.0, min(1.0, (90.0 - remaining_sec) / 90.0))
            alignment_strength = max(0.0, min(1.0, polymarket_alignment))
            time_decay_pts = 10.0 * time_pressure * alignment_strength

        # ── 7. Window persistence bonus (0-12 pts) ────────────────────────────
        # A delta that has been sustained as the window progresses is far more
        # reliable than one that just appeared.  The bonus scales linearly from
        # 0 at T+30s to 12 at T+270s (last 30s of entry window), weighted by
        # delta magnitude so small moves don't earn persistence credit.
        #
        #   elapsed  | 0.10%+ delta | 0.05% delta | 0.03% delta
        #   ─────────┼──────────────┼─────────────┼────────────
        #   T+30s    |    0.0 pts   |   0.0 pts   |  0.0 pts
        #   T+60s    |    1.5 pts   |   0.75 pts  |  0.45 pts
        #   T+120s   |    4.5 pts   |   2.25 pts  |  1.35 pts
        #   T+180s   |    7.5 pts   |   3.75 pts  |  2.25 pts
        #   T+240s   |   10.5 pts   |   5.25 pts  |  3.15 pts
        #   T+270s   |   12.0 pts   |   6.0 pts   |  3.6 pts
        persistence_pts = 0.0
        if elapsed_sec >= 30.0 and abs(window_delta_pct) >= 0.03:
            # progress: 0.0 at T+30s → 1.0 at T+270s
            progress = min(1.0, (elapsed_sec - 30.0) / 240.0)
            # delta_factor: full bonus from 0.10%+, scales down for smaller moves
            delta_factor = min(1.0, abs(window_delta_pct) / 0.10)
            persistence_pts = 12.0 * progress * delta_factor

        # ── 8. Cross-asset alignment (-8..+5 pts) ─────────────────────────────
        # BTC and ETH have ~85% directional correlation on 5-min windows.
        # When they move opposite directions the move is likely idiosyncratic
        # noise (higher reversal risk). When they align it mildly confirms.
        #
        # cross_asset_alignment: +1 = other asset moving same direction as us
        #                         0 = other asset flat / no data
        #                        -1 = other asset moving opposite direction
        cross_asset_pts = 0.0
        if cross_asset_alignment > 0.1:
            # Aligned: small bonus (+5 at full alignment)
            cross_asset_pts = 5.0 * cross_asset_alignment
        elif cross_asset_alignment < -0.1:
            # Diverging: meaningful penalty (-8 at full divergence)
            cross_asset_pts = 8.0 * cross_asset_alignment  # negative value

        # ── Total ─────────────────────────────────────────────────────────────
        total = (signal_pts + agent_pts + delta_pts + regime_pts
                 + momentum_pts + time_decay_pts + persistence_pts
                 + cross_asset_pts)
        total = max(0.0, min(100.0, total))

        breakdown = ConfidenceBreakdown(
            signal_contribution=signal_pts,
            agent_contribution=agent_pts,
            delta_contribution=delta_pts,
            regime_contribution=regime_pts,
            total=total,
            momentum_contribution=momentum_pts,
            time_decay_contribution=time_decay_pts,
            persistence_contribution=persistence_pts,
            cross_asset_contribution=cross_asset_pts,
        )

        if breakdown.should_trade:
            logger.info(
                f"Confidence: {total:.1f} (signal={signal_pts:.0f}, "
                f"agents={agent_pts:.0f}, delta={delta_pts:.0f}, "
                f"regime={regime_pts:.0f}, momentum={momentum_pts:.1f}, "
                f"persist={persistence_pts:.1f}, "
                f"time_decay={time_decay_pts:.1f}, "
                f"cross_asset={cross_asset_pts:+.1f}) → TRADE"
            )
        else:
            logger.debug(
                f"Confidence: {total:.1f} < {cfg.min_confidence_score} → SKIP"
            )

        return breakdown
