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
from strategy.indicators import atr as compute_atr
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

        if current_price <= 0:
            return self._no_trade("No live price yet — data feed not ready")

        # ── 1. Window delta ──────────────────────────────────────────────────
        window_delta_pct = (current_price - open_price) / open_price * 100

        # Delta acceleration: positive value = move is growing in same direction
        delta_acceleration = window_delta_pct - self._prev_window_delta_pct
        self._prev_window_delta_pct = window_delta_pct

        # ── 2. Market data ───────────────────────────────────────────────────
        df_1m = self._candles.get_dataframe("1m")
        df_5s = self._candles.get_dataframe("5s")

        # Dynamic delta gate: scale minimum required delta by current volatility.
        # In a high-volatility environment, a small delta is noise; in a quiet
        # market, even a 0.01% move is a meaningful directional signal.
        # Reference ATR: 0.03% per 1-min candle = "typical" BTC micro-move.
        # We allow the threshold to shrink in quiet markets (down to 0.5× base)
        # and grow in volatile markets (up to 2.5× base) to filter noise.
        REF_ATR = 0.03
        if len(df_1m) >= 14:
            current_atr = compute_atr(df_1m, period=14)
            if current_atr > 0:
                vol_scale = max(0.5, min(2.5, current_atr / REF_ATR))
                dynamic_min_delta = cfg.min_window_delta_pct * vol_scale
            else:
                dynamic_min_delta = cfg.min_window_delta_pct
        else:
            current_atr = 0.0
            dynamic_min_delta = cfg.min_window_delta_pct

        if abs(window_delta_pct) < dynamic_min_delta:
            return self._no_trade(
                f"Window delta too small: {window_delta_pct:.4f}% "
                f"(vol-adj min={dynamic_min_delta:.4f}%, ATR={current_atr:.4f}%) — no edge"
            )
        agg = self._aggregator.get_aggregated()

        # Order book imbalance for the YES token of current market
        ob_imbalance = await self._get_order_book_imbalance()
        oracle_delta = agg.cex_oracle_delta_pct

        # Polymarket odds alignment: how strongly do current PM odds confirm direction?
        # YES token mid-price ≈ market's implied P(BTC UP by window end).
        # When close to deadline, this is a high-quality confirming signal because
        # there's little time for the outcome to reverse.
        polymarket_alignment = 0.0
        if self._current_yes_token_id:
            book = self._poly_ws.get_order_book(self._current_yes_token_id)
            if book and book.best_bid > 0 and book.best_ask < 1.0:
                yes_mid = book.mid_price
                # Scale: 0.5 = no info, 0.8 = strong UP, 0.2 = strong DOWN
                # Convert to [-1, +1] centered at 0.5
                pm_signal = (yes_mid - 0.5) * 2.0
                # Alignment = PM signal in same direction as our window delta
                window_direction_sign = 1.0 if window_delta_pct > 0 else -1.0
                polymarket_alignment = float(max(-1.0, min(1.0, pm_signal * window_direction_sign)))

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

        # In perfectly flat markets with truly no signal, skip.
        # Use SNR so a tiny delta in an ultra-low-vol environment isn't thrown out.
        # Threshold is kept low (0.004%) so early-window entries at T+5-10s with
        # tiny BTC delta still get through — the regime/agent signals carry the load.
        if regime.bonus == 0.0:
            snr = abs(window_delta_pct) / regime.volatility if regime.volatility > 0 else 0.0
            if abs(window_delta_pct) < 0.004 and snr < 0.8:
                return self._no_trade(
                    f"Ranging market + no signal "
                    f"(delta={window_delta_pct:.3f}%, SNR={snr:.2f}) → skip"
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
            regime_volatility=regime.volatility,
            remaining_sec=window.remaining_sec,
            polymarket_alignment=polymarket_alignment,
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

    async def _get_order_book_imbalance(self) -> float | None:
        """
        Get order book imbalance for the current market's YES token.
        Tries the live WS cache first (zero-latency), falls back to a REST snapshot.
        Returns None only if both sources fail.
        """
        if not self._current_yes_token_id:
            return None

        # Fast path: WS has a cached book
        book = self._poly_ws.get_order_book(self._current_yes_token_id)
        if book:
            return book.imbalance_ratio

        # Fallback: REST snapshot (adds ~100-200ms but always available)
        try:
            from data.polymarket_rest import PolymarketRestClient
            async with PolymarketRestClient() as rest:
                data = await rest.get_order_book(self._current_yes_token_id)
            bids = [(float(b["price"]), float(b["size"])) for b in data.get("bids", [])]
            asks = [(float(a["price"]), float(a["size"])) for a in data.get("asks", [])]
            if not bids and not asks:
                return None
            # Price-weighted imbalance (same formula as OrderBook.imbalance_ratio)
            mid = ((max(p for p, _ in bids) + min(p for p, _ in asks)) / 2) if bids and asks else 0.0
            eps = 1e-4
            if mid > 0:
                w_bids = sum(s / (abs(mid - p) + eps) for p, s in bids[:10])
                w_asks = sum(s / (abs(p - mid) + eps) for p, s in asks[:10])
            else:
                w_bids = sum(s for _, s in bids[:10])
                w_asks = sum(s for _, s in asks[:10])
            total = w_bids + w_asks
            return (w_bids - w_asks) / total if total > 0 else 0.0
        except Exception as exc:
            logger.debug(f"Order book REST fallback failed: {exc}")
            return None

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
