"""
Position sizer — Kelly criterion with fractional Kelly and guardrails.

Kelly criterion: f* = (p × b - q) / b
Where:
  p = probability of win
  q = 1 - p
  b = net odds (how much you win per dollar risked)

For binary prediction markets:
  b = (1 - price) / price  (buying YES token at price p)
  Win: token worth $1.00, profit = (1-price)/price per dollar
  Lose: token worth $0.00, lose entire stake
"""

from __future__ import annotations

from dataclasses import dataclass

from core.config import get_config
from core.logger import get_logger

logger = get_logger(__name__)
cfg = get_config()


@dataclass
class SizingResult:
    """Position sizing recommendation."""
    size_usd: float
    kelly_fraction: float
    raw_kelly: float        # Full Kelly (before fractional adjustment)
    adjustments: list[str]  # List of applied adjustments

    def to_dict(self) -> dict:
        return {
            "size_usd": round(self.size_usd, 2),
            "kelly_fraction": round(self.kelly_fraction, 4),
            "raw_kelly": round(self.raw_kelly, 4),
            "adjustments": self.adjustments,
        }


class PositionSizer:
    """
    Fractional Kelly position sizing with multiple safety guardrails.

    Quarter-Kelly (0.25 × Kelly) is conservative: sacrifices ~6% of
    optimal growth rate for significantly lower variance and drawdown.
    """

    def __init__(self) -> None:
        self._kelly_fraction = cfg.kelly_fraction       # Default: 0.25
        self._max_position_pct = cfg.max_position_pct  # Max 10% of balance

    def calculate(
        self,
        balance: float,
        confidence: float,
        win_rate: float,
        token_price: float,
        consecutive_losses: int = 0,
        drawdown_pct: float = 0.0,
    ) -> SizingResult:
        """
        Calculate position size.

        Args:
            balance:            Current USDC balance
            confidence:         Signal confidence (0-100)
            win_rate:           Historical win rate (0-1), default 0.55
            token_price:        Token price we're buying (0-1)
            consecutive_losses: Recent consecutive losses (for scaling down)
            drawdown_pct:       Current drawdown from peak (%)

        Returns:
            SizingResult with recommended position size
        """
        adjustments = []

        # ── Kelly calculation ─────────────────────────────────────────────────
        # Net odds on a binary prediction market
        # Buying YES at price p: win (1-p)/p, lose 1
        if token_price <= 0 or token_price >= 1:
            token_price = 0.70   # Fallback

        net_odds = (1.0 - token_price) / token_price

        # Win probability = market's implied probability + our edge over the market.
        # For prediction markets, Kelly is positive only when win_prob > token_price.
        # We anchor to the market price and add a confidence-based edge:
        #   confidence 50  → 0% edge (no conviction above market)
        #   confidence 75  → +12.5% edge
        #   confidence 100 → +25% edge
        # win_rate from historical data scales the edge (better track record → bigger edge).
        # When no trades exist yet (win_rate=0), use a conservative prior of 0.52.
        #
        # Cap: must stay above token_price (to guarantee positive Kelly) but
        # never exceed 0.99 (avoid division issues near 1.0). A hard 0.95 cap
        # caused negative Kelly for any token priced above ~0.90, blocking all
        # late-window trades on clearly-trending markets.
        effective_win_rate = win_rate if win_rate > 0 else 0.52
        edge = max(0.0, (confidence - 50) / 100 * 0.5) * (effective_win_rate / 0.55)
        win_prob = min(0.99, token_price + edge)

        # Kelly fraction
        q = 1.0 - win_prob
        raw_kelly = (win_prob * net_odds - q) / net_odds

        # Dynamic Kelly fraction: scales with confidence.
        # At confidence=50 → 1.0× base fraction (unchanged).
        # At confidence=100 → 1.4× (more certain about edge → size up).
        # At confidence=0  → 0.6× (low certainty → size down).
        confidence_scale = 0.6 + 0.8 * (confidence / 100.0)
        dynamic_kelly_frac = self._kelly_fraction * confidence_scale
        kelly = raw_kelly * dynamic_kelly_frac
        adjustments.append(
            f"Dynamic Kelly × {dynamic_kelly_frac:.3f} "
            f"(base {self._kelly_fraction} × {confidence_scale:.2f} conf-scale)"
        )

        # ── Guardrails ────────────────────────────────────────────────────────

        # 1. Max position size (10% of balance)
        max_fraction = self._max_position_pct
        if kelly > max_fraction:
            kelly = max_fraction
            adjustments.append(f"Capped at {max_fraction:.0%} of balance")

        # 2. Scale down for consecutive losses
        if consecutive_losses >= 3:
            loss_factor = max(0.3, 1.0 - (consecutive_losses - 2) * 0.2)
            kelly *= loss_factor
            adjustments.append(
                f"Loss streak ×{loss_factor:.2f} ({consecutive_losses} losses)"
            )

        # 3. Scale down for drawdown
        if drawdown_pct > 5.0:
            dd_factor = max(0.3, 1.0 - (drawdown_pct - 5.0) / 20.0)
            kelly *= dd_factor
            adjustments.append(f"Drawdown ×{dd_factor:.2f} ({drawdown_pct:.1f}% DD)")

        # 4. Negative Kelly → skip trade
        if kelly <= 0 or raw_kelly <= 0:
            logger.info(
                f"Negative Kelly: token_price={token_price:.3f}, "
                f"win_prob={win_prob:.3f}, edge={edge:.3f}, "
                f"raw_kelly={raw_kelly:.4f} — market has priced in the move"
            )
            return SizingResult(
                size_usd=0.0,
                kelly_fraction=kelly,
                raw_kelly=raw_kelly,
                adjustments=["Negative Kelly — no edge, skip"],
            )

        # ── Final size ────────────────────────────────────────────────────────
        size_usd = balance * kelly

        # Hard constraint first: funds available above minimum reserve
        available_usd = max(0.0, balance - cfg.min_usdc_balance)

        # Platform minimum: larger of $10 or 0.5% of balance
        platform_min = max(10.0, balance * 0.005)

        if available_usd < platform_min:
            return SizingResult(
                size_usd=0.0,
                kelly_fraction=kelly,
                raw_kelly=raw_kelly,
                adjustments=adjustments + [
                    f"Insufficient available balance (${available_usd:.2f} < ${platform_min:.2f} minimum)"
                ],
            )

        # Ceiling: smaller of available funds and max position pct
        ceiling_usd = min(available_usd, balance * self._max_position_pct)
        if size_usd > ceiling_usd:
            size_usd = ceiling_usd
            adjustments.append(f"Capped at ${ceiling_usd:.2f}")

        # Soft floor: platform minimum — let Kelly go below trade_amount_usd when
        # edge is genuinely small, but never below exchange minimum.
        if size_usd < platform_min:
            size_usd = platform_min
            adjustments.append(f"Floored at platform minimum ${platform_min:.2f}")

        return SizingResult(
            size_usd=round(size_usd, 2),
            kelly_fraction=kelly,
            raw_kelly=raw_kelly,
            adjustments=adjustments,
        )
