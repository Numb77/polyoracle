"""
Late-Window Directional Strategy — the primary trading strategy.

Waits until T-30s to T-5s when BTC's direction is largely determined,
then places a high-confidence bet on the confirmed direction.
"""

from __future__ import annotations

from core.clock import WindowState
from core.config import get_config
from core.logger import get_logger
from data.candle_builder import CandleBuilder
from data.aggregator import PriceAggregator
from data.polymarket_ws import PolymarketWebSocket
from data.chainlink_oracle import ChainlinkOracle
from strategy.base import BaseStrategy, TradeDecision
from strategy.signals import SignalCombiner, CompositeSignal
from strategy.confidence import ConfidenceEngine, ConfidenceBreakdown
from strategy.market_regime import detect_regime, RegimeResult
from agents.consensus import ConsensusEngine, ConsensusResult

logger = get_logger(__name__)
cfg = get_config()


class LateWindowStrategy(BaseStrategy):
    """
    The primary strategy: waits for the last 30 seconds of each 5-minute window,
    checks that BTC's direction is clear, and bets on it.
    """

    def __init__(
        self,
        candle_builder: CandleBuilder,
        aggregator: PriceAggregator,
        poly_ws: PolymarketWebSocket,
        oracle: ChainlinkOracle,
        consensus_engine: ConsensusEngine,
    ) -> None:
        self._candles = candle_builder
        self._aggregator = aggregator
        self._poly_ws = poly_ws
        self._oracle = oracle
        self._consensus = consensus_engine

        self._signal_combiner = SignalCombiner()
        self._confidence_engine = ConfidenceEngine()

        # Per-window state
        self._window_open_price: float = 0.0
        self._last_evaluation: TradeDecision | None = None
        self._last_consensus: ConsensusResult | None = None
        self._current_yes_token_id: str = ""
        self._current_no_token_id: str = ""
        self._prev_window_delta_pct: float = 0.0  # For momentum acceleration

    @property
    def last_consensus(self) -> ConsensusResult | None:
        return self._last_consensus

    @property
    def name(self) -> str:
        return "late_window"

    @property
    def window_open_price(self) -> float:
        return self._window_open_price

    def update_window_open_price(self, price: float) -> None:
        """Set the window open price (called when a new window opens)."""
        self._window_open_price = price
        self._prev_window_delta_pct = 0.0  # Reset acceleration tracking for new window
        logger.info(f"Window open price set: ${price:,.2f}")

    def set_current_market_tokens(self, yes_token_id: str, no_token_id: str) -> None:
        """Set the YES/NO token IDs for the current window's market."""
        self._current_yes_token_id = yes_token_id
        self._current_no_token_id = no_token_id

    async def evaluate(self, window: WindowState) -> TradeDecision:
        """
        Full strategy evaluation pipeline:
        1. Compute window delta
        2. Build composite signal from all indicators
        3. Get agent consensus
        4. Compute confidence score
        5. Return trade decision
        """
        current_price = self._aggregator.current_price
        open_price = self._window_open_price or window.open_price

        if open_price <= 0:
            return self._no_trade("No window open price available")

        # ── 1. Window delta ──────────────────────────────────────────────────
        window_delta_pct = (current_price - open_price) / open_price * 100

        # Delta acceleration: positive value = move is growing in same direction
        delta_acceleration = window_delta_pct - self._prev_window_delta_pct
        self._prev_window_delta_pct = window_delta_pct

        # Early exit: if delta is truly tiny, signal is noise
        if abs(window_delta_pct) < 0.002:
            return self._no_trade(
                f"Window delta too small: {window_delta_pct:.4f}% — noise"
            )

        # ── 2. Market data ───────────────────────────────────────────────────
        df_1m = self._candles.get_dataframe("1m")
        df_5s = self._candles.get_dataframe("5s")
        agg = self._aggregator.get_aggregated()

        # Order book imbalance for the YES token of current market
        ob_imbalance = self._get_order_book_imbalance(window)
        oracle_delta = agg.cex_oracle_delta_pct

        # ── 3. Composite signal ──────────────────────────────────────────────
        signal = self._signal_combiner.compute(
            window_delta_pct=window_delta_pct,
            df_1m=df_1m,
            df_5s=df_5s,
            order_book_imbalance=ob_imbalance,
            oracle_delta_pct=oracle_delta,
        )

        # Reject if signal direction conflicts with window delta
        # (window delta is our anchor — other signals are confirmatory)
        window_direction = "UP" if window_delta_pct > 0 else "DOWN"
        if signal.direction != "NEUTRAL" and signal.direction != window_direction:
            # Mixed signals — reduce confidence but don't skip yet
            logger.debug(
                f"Signal direction {signal.direction} conflicts with "
                f"window delta direction {window_direction}"
            )

        # ── 4. Market regime ─────────────────────────────────────────────────
        regime = detect_regime(df_1m)

        # Don't trade in perfectly flat markets with no signal.
        # 5-minute BTC windows rarely move >0.1% so we keep the bar low (0.02%).
        if regime.bonus == 0.0 and abs(window_delta_pct) < 0.02:
            return self._no_trade(
                f"Ranging market + tiny delta ({window_delta_pct:.3f}%) → skip"
            )

        # ── 5. Agent consensus ───────────────────────────────────────────────
        consensus = await self._consensus.get_consensus(
            window_delta_pct=window_delta_pct,
            signal=signal,
            df_1m=df_1m,
            df_5s=df_5s,
            ob_imbalance=ob_imbalance,
            oracle_delta_pct=oracle_delta,
            atr_pct=regime.volatility,
        )

        self._last_consensus = consensus

        # ── 6. Confidence score ──────────────────────────────────────────────
        confidence = self._confidence_engine.compute(
            composite_signal=signal,
            window_delta_pct=window_delta_pct,
            agent_agreement_ratio=consensus.agreement_ratio,
            regime_bonus=regime.bonus,
            delta_acceleration=delta_acceleration,
        )

        # ── 7. Direction: always follow window delta ─────────────────────────
        # The window delta is the ground truth. Agents/indicators are confirmatory.
        direction = window_direction

        if not confidence.should_trade:
            return TradeDecision(
                should_trade=False,
                direction=direction,
                confidence=confidence,
                signal=signal,
                reason=f"Confidence {confidence.total:.0f} < {cfg.min_confidence_score}",
            )

        decision = TradeDecision(
            should_trade=True,
            direction=direction,
            confidence=confidence,
            signal=signal,
            reason=(
                f"Strong {direction} signal: delta={window_delta_pct:+.3f}%, "
                f"confidence={confidence.total:.0f}, "
                f"agents={consensus.agreement_ratio:.0%}"
            ),
        )
        self._last_evaluation = decision
        logger.trade(  # type: ignore[attr-defined]
            f"Trade signal: {direction} @ confidence={confidence.total:.0f} "
            f"(delta={window_delta_pct:+.3f}%, {regime.regime.name})"
        )
        return decision

    def _get_order_book_imbalance(self, window: WindowState) -> float:
        """Get order book imbalance from Polymarket WS for the current market's YES token."""
        if self._current_yes_token_id:
            book = self._poly_ws.get_order_book(self._current_yes_token_id)
            if book:
                return book.imbalance_ratio
        return 0.0

    def _no_trade(self, reason: str) -> TradeDecision:
        """Return a 'no trade' decision."""
        from strategy.signals import CompositeSignal, SignalComponent
        from strategy.confidence import ConfidenceBreakdown

        dummy_signal = CompositeSignal(
            components=[],
            window_delta_score=0.0,
            order_book_score=0.0,
            oracle_delta_score=0.0,
        )
        dummy_confidence = ConfidenceBreakdown(
            signal_contribution=0.0,
            agent_contribution=0.0,
            delta_contribution=0.0,
            regime_contribution=0.0,
            total=0.0,
        )
        return TradeDecision(
            should_trade=False,
            direction="NEUTRAL",
            confidence=dummy_confidence,
            signal=dummy_signal,
            reason=reason,
        )
