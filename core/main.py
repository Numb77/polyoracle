"""
PolyOracle — Main orchestrator loop.

Wires together all components and runs the main trading loop:
  1. Start data feeds (Binance WS, Polymarket WS, Chainlink oracle)
  2. Run the window clock
  3. At T-30s: begin strategy evaluation
  4. At T-10s: if confidence high enough, execute trade
  5. At T+0s: wait for resolution, auto-claim if won
  6. Push all state updates to dashboard via WebSocket

Usage:
    python -m core.main           # Live trading (requires PAPER_MODE=false in .env)
    python -m core.main --paper   # Paper trading (default)
    python -m core.main --help
"""

from __future__ import annotations

import asyncio
import signal
import sys
import time
from typing import Optional

import click

from core.clock import WindowClock, WindowPhase, WindowState
from core.config import get_config
from core.logger import get_logger, setup_logging
from data.aggregator import PriceAggregator
from data.binance_ws import BinanceWebSocket, BtcTick, get_window_open_price, get_btc_window_open_price
from data.candle_builder import CandleBuilder
from data.chainlink_oracle import ChainlinkOracle
from data.gamma_api import GammaClient
from data.polymarket_rest import PolymarketRestClient
from data.polymarket_ws import PolymarketWebSocket
from agents.consensus import ConsensusEngine
from agents.meta_learner import MetaLearner
from agents.momentum_agent import MomentumAgent
from agents.mean_reversion_agent import MeanReversionAgent
from agents.volatility_agent import VolatilityAgent
from agents.orderflow_agent import OrderFlowAgent
from agents.oracle_agent import OracleAgent
from execution.claimer import Claimer
from execution.fee_calculator import FeeCalculator
from execution.order_manager import OrderManager, OrderStatus
from execution.polymarket_executor import PolymarketExecutor
from execution.token_resolver import TokenResolver
from execution.wallet import Wallet
from risk.circuit_breaker import CircuitBreaker, CircuitTier
from risk.drawdown_monitor import DrawdownMonitor
from risk.exposure_manager import ExposureManager
from risk.pnl_tracker import PnlTracker
from risk.position_sizer import PositionSizer
from strategy.late_window import LateWindowStrategy
from websocket_server.server import DashboardServer

logger = get_logger(__name__)
cfg = get_config()


class PolyOracle:
    """
    Main bot orchestrator. Wires all components and runs the event loop.
    """

    def __init__(self, paper_mode: bool | None = None) -> None:
        # Override paper mode if explicitly specified
        if paper_mode is not None:
            cfg.paper_mode = paper_mode

        self._running = False
        self._current_window_ts = 0
        self._last_trade_votes = []

        # ── Data layer — BTC ──────────────────────────────────────────────────
        self._aggregator = PriceAggregator()
        self._candles = CandleBuilder()
        self._binance_ws = BinanceWebSocket(cfg.binance_ws_url)
        self._poly_ws = PolymarketWebSocket()
        self._oracle = ChainlinkOracle(cfg.chainlink_btc_usd_proxy)
        self._rest_client = PolymarketRestClient()
        self._gamma = GammaClient(self._rest_client)
        self._token_resolver = TokenResolver(self._gamma, self._rest_client, asset="btc")

        # ── Data layer — ETH ──────────────────────────────────────────────────
        self._eth_aggregator = PriceAggregator()
        self._eth_candles = CandleBuilder()
        self._eth_binance_ws = BinanceWebSocket(cfg.binance_eth_ws_url)
        self._eth_oracle = ChainlinkOracle(cfg.chainlink_eth_usd_proxy)
        self._eth_token_resolver = TokenResolver(self._gamma, self._rest_client, asset="eth")

        # ── Window clock ──────────────────────────────────────────────────────
        self._clock = WindowClock(
            entry_window_start_sec=cfg.entry_window_start_sec,
            trading_window_start_sec=cfg.trading_window_start_sec,
            entry_deadline_sec=cfg.entry_deadline_sec,
        )

        # ── Risk layer ────────────────────────────────────────────────────────
        initial_balance = cfg.paper_initial_balance if cfg.paper_mode else 0.0
        self._pnl = PnlTracker(initial_balance)
        self._drawdown = DrawdownMonitor(initial_balance)
        self._circuit = CircuitBreaker()
        self._exposure = ExposureManager()
        self._sizer = PositionSizer()
        self._fee_calc = FeeCalculator()

        # ── Agent system — BTC ────────────────────────────────────────────────
        self._meta_learner = MetaLearner()
        self._agents = [
            MomentumAgent(),
            MeanReversionAgent(),
            VolatilityAgent(),
            OrderFlowAgent(),
            OracleAgent(),
        ]
        self._consensus = ConsensusEngine(self._agents, self._meta_learner)

        # ── Agent system — ETH (separate meta-learner + consensus) ────────────
        self._eth_meta_learner = MetaLearner()
        self._eth_agents = [
            MomentumAgent(),
            MeanReversionAgent(),
            VolatilityAgent(),
            OrderFlowAgent(),
            OracleAgent(),
        ]
        self._eth_consensus = ConsensusEngine(self._eth_agents, self._eth_meta_learner)

        # ── Execution layer ───────────────────────────────────────────────────
        self._order_manager = OrderManager()
        self._eth_order_manager = OrderManager()
        self._claimer = Claimer(self._order_manager)
        self._eth_claimer = Claimer(self._eth_order_manager)

        # Wallet and executor (may be None in paper mode without private key)
        self._wallet: Wallet | None = None
        self._executor: PolymarketExecutor | None = None
        self._eth_executor: PolymarketExecutor | None = None
        self._balance = initial_balance  # shared USDC pool for both BTC and ETH

        # ── Strategy — BTC ────────────────────────────────────────────────────
        self._strategy = LateWindowStrategy(
            candle_builder=self._candles,
            aggregator=self._aggregator,
            poly_ws=self._poly_ws,
            oracle=self._oracle,
            consensus_engine=self._consensus,
        )

        # ── Strategy — ETH ────────────────────────────────────────────────────
        self._eth_strategy = LateWindowStrategy(
            candle_builder=self._eth_candles,
            aggregator=self._eth_aggregator,
            poly_ws=self._poly_ws,
            oracle=self._eth_oracle,
            consensus_engine=self._eth_consensus,
        )

        # ETH last trade votes for meta-learner feedback
        self._eth_last_trade_votes = []

        # ── Dashboard ─────────────────────────────────────────────────────────
        self._dashboard = DashboardServer()
        self._dashboard.set_command_handler(self._handle_command)

    # ── Startup ───────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Initialize all components and verify connectivity."""
        logger.info(
            f"Starting PolyOracle {'[PAPER MODE]' if cfg.paper_mode else '[LIVE MODE]'}"
        )

        # Initialize wallet
        if cfg.has_wallet():
            try:
                self._wallet = Wallet()
                balances = await self._wallet.log_balances()
                # In live mode use real USDC balance; in paper mode keep simulated balance
                if not cfg.paper_mode:
                    self._balance = balances["usdc"]
                    self._drawdown = DrawdownMonitor(self._balance)
                    self._pnl = PnlTracker(self._balance)
            except Exception as exc:
                logger.error(f"Wallet initialization failed: {exc}")
                if not cfg.paper_mode:
                    raise

        if self._wallet:
            self._executor = PolymarketExecutor(
                wallet=self._wallet,
                order_manager=self._order_manager,
                fee_calculator=self._fee_calc,
            )
            self._eth_executor = PolymarketExecutor(
                wallet=self._wallet,
                order_manager=self._eth_order_manager,
                fee_calculator=self._fee_calc,
            )
        else:
            self._executor = PolymarketExecutor(
                wallet=None,  # type: ignore[arg-type]
                order_manager=self._order_manager,
                fee_calculator=self._fee_calc,
            )
            self._eth_executor = PolymarketExecutor(
                wallet=None,  # type: ignore[arg-type]
                order_manager=self._eth_order_manager,
                fee_calculator=self._fee_calc,
            )

        # Subscribe to Binance tick updates
        self._binance_ws.subscribe(self._on_tick)
        self._eth_binance_ws.subscribe(self._on_eth_tick)

        # Register clock callbacks
        self._clock.on_window_open(self._on_window_open)
        self._clock.on_phase_change(self._on_phase_change)
        self._clock.on_tick(self._on_clock_tick)
        self._clock.on_window_close(self._on_window_close)

        self._running = True
        logger.info("PolyOracle initialized successfully")

    # ── Main run loop ─────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Run all tasks concurrently."""
        await self.start()

        tasks = [
            asyncio.create_task(self._binance_ws.run(), name="binance_ws"),
            asyncio.create_task(self._eth_binance_ws.run(), name="eth_binance_ws"),
            asyncio.create_task(self._clock.run(), name="clock"),
            asyncio.create_task(self._oracle.start(), name="oracle"),
            asyncio.create_task(self._eth_oracle.start(), name="eth_oracle"),
            asyncio.create_task(self._dashboard.start(), name="dashboard"),
            asyncio.create_task(self._poly_ws.run(), name="poly_ws"),
            asyncio.create_task(self._heartbeat_loop(), name="heartbeat"),
        ]

        logger.info("All tasks started. PolyOracle running.")
        self._dashboard.push_log("INFO", "main", "PolyOracle started")

        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        except asyncio.CancelledError:
            logger.info("Tasks cancelled — shutting down")
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        """Graceful shutdown."""
        logger.info("Shutting down PolyOracle...")
        self._running = False

        # Cancel all open orders on shutdown
        if self._executor:
            await self._executor.cancel_all_open()
        if self._eth_executor:
            await self._eth_executor.cancel_all_open()

        self._binance_ws.stop()
        self._eth_binance_ws.stop()
        self._poly_ws.stop()
        self._oracle.stop()
        self._eth_oracle.stop()
        self._clock.stop()
        self._dashboard.stop()

        stats = self._pnl.get_stats()
        logger.info(
            f"Final stats: trades={stats.total_trades}, "
            f"win_rate={stats.win_rate:.1%}, "
            f"total_pnl=${stats.total_pnl:+.2f}"
        )

    # ── Tick handler ──────────────────────────────────────────────────────────

    async def _on_tick(self, tick: BtcTick) -> None:
        """Handle a new BTC price tick."""
        self._aggregator.update_binance(tick.price, tick.qty)
        self._clock.update_price(tick.price)

        oracle = self._oracle.latest
        if oracle:
            self._aggregator.update_oracle(oracle.price, oracle.updated_at)

        await self._candles.on_tick(tick)

    async def _on_eth_tick(self, tick: BtcTick) -> None:
        """Handle a new ETH price tick."""
        self._eth_aggregator.update_binance(tick.price, tick.qty)

        oracle = self._eth_oracle.latest
        if oracle:
            self._eth_aggregator.update_oracle(oracle.price, oracle.updated_at)

        await self._eth_candles.on_tick(tick)

    # ── Clock callbacks ───────────────────────────────────────────────────────

    async def _on_window_open(self, window: WindowState) -> None:
        """New 5-minute window opened."""
        # Fetch BTC open price
        btc_price = await get_window_open_price("BTCUSDT", window.window_ts)
        if btc_price <= 0:
            btc_price = self._aggregator.current_price
            logger.warning(
                f"BTC kline open unavailable for {window.window_slug}, "
                f"falling back to live price ${btc_price:,.2f}"
            )

        self._clock.set_window_open_price(btc_price)
        self._strategy.update_window_open_price(btc_price)
        self._current_window_ts = window.window_ts

        # Fetch ETH open price
        eth_price = await get_window_open_price("ETHUSDT", window.window_ts)
        if eth_price <= 0:
            eth_price = self._eth_aggregator.current_price
            logger.warning(
                f"ETH kline open unavailable for {window.window_slug}, "
                f"falling back to live ETH price ${eth_price:,.2f}"
            )
        self._eth_strategy.update_window_open_price(eth_price)

        logger.info(
            f"Window {window.window_slug} opened: "
            f"BTC=${btc_price:,.2f} ETH=${eth_price:,.2f}"
        )
        self._dashboard.push("window_state", window.to_dict())
        self._dashboard.push("eth_window_state", {**window.to_dict(), "open_price": eth_price, "current_price": eth_price, "delta_pct": 0.0})
        self._dashboard.push_log(
            "INFO", "clock",
            f"New window: {window.window_slug} | BTC ${btc_price:,.2f} | ETH ${eth_price:,.2f}"
        )

        # Subscribe to Polymarket order books for this window
        await self._subscribe_market(window)
        await self._eth_subscribe_market(window)

    async def _on_phase_change(self, window: WindowState) -> None:
        """Window phase transitioned."""
        self._dashboard.push("window_state", window.to_dict())
        self._dashboard.push_log(
            "INFO", "clock",
            f"Phase → {window.phase.name} ({window.remaining_sec:.0f}s remaining)"
        )

        # Entry window: start evaluating
        if window.phase == WindowPhase.EVALUATING:
            await asyncio.gather(
                self._evaluate(window),
                self._eth_evaluate(window),
                return_exceptions=True,
            )

        # Trading window: decision point
        elif window.phase == WindowPhase.TRADING:
            await asyncio.gather(
                self._decide(window),
                self._eth_decide(window),
                return_exceptions=True,
            )

        # Hard deadline: fire or forever hold your peace
        elif window.phase == WindowPhase.DEADLINE:
            await asyncio.gather(
                self._deadline_trade(window),
                self._eth_deadline_trade(window),
                return_exceptions=True,
            )

    async def _on_clock_tick(self, window: WindowState) -> None:
        """Every-second tick — push state to dashboard."""
        now = time.time()

        self._dashboard.push("window_state", window.to_dict())

        # Push BTC price tick
        btc_price = self._aggregator.current_price
        self._dashboard.push("tick", {"price": btc_price, "timestamp": now})

        # Push ETH window state and tick
        eth_price = self._eth_aggregator.current_price
        if eth_price > 0:
            eth_open = self._eth_strategy.window_open_price
            eth_delta = ((eth_price - eth_open) / eth_open * 100) if eth_open > 0 else 0.0
            eth_window_data = {**window.to_dict(), "open_price": eth_open, "current_price": eth_price, "delta_pct": round(eth_delta, 4)}
            self._dashboard.push("eth_window_state", eth_window_data)
            self._dashboard.push("eth_tick", {"price": eth_price, "timestamp": now})

    async def _on_window_close(self, window: WindowState) -> None:
        """Window closed — cancel any unfilled GTC orders, then await resolution."""
        logger.info(f"Window {window.window_slug} closed. Awaiting resolution...")
        self._dashboard.push_log(
            "INFO", "clock", f"Window closed: {window.window_slug}"
        )

        # Cancel any GTC orders that didn't fill before the window ended
        cancelled = await self._executor.cancel_all_open()
        if cancelled:
            logger.info(f"BTC: Cancelled {cancelled} unfilled GTC order(s) at window close")

        eth_cancelled = await self._eth_executor.cancel_all_open()
        if eth_cancelled:
            logger.info(f"ETH: Cancelled {eth_cancelled} unfilled GTC order(s) at window close")

        # Process claims after a short delay (let resolution finalize)
        asyncio.create_task(self._process_window_resolution(window))
        asyncio.create_task(self._eth_process_window_resolution(window))

    # ── Strategy evaluation ───────────────────────────────────────────────────

    async def _evaluate(self, window: WindowState) -> None:
        """Run strategy evaluation (T-30s). Warm up agents."""
        try:
            decision = await self._strategy.evaluate(window)
            logger.debug(
                f"T-30s evaluation: {decision.direction} "
                f"conf={decision.confidence.total:.0f}"
            )
            self._dashboard.push("confidence", decision.confidence.to_dict())
            if self._strategy.last_consensus:
                self._dashboard.push("agent_votes", self._strategy.last_consensus.to_dict())
        except Exception as exc:
            logger.error(f"Strategy evaluation error: {exc}", exc_info=True)

    async def _decide(self, window: WindowState) -> None:
        """Decision point (T-10s). Execute if confident."""
        await self._maybe_trade(window, is_deadline=False)

    async def _deadline_trade(self, window: WindowState) -> None:
        """Hard deadline (T-5s). Use best available signal."""
        await self._maybe_trade(window, is_deadline=True)

    # ── ETH strategy wrappers ─────────────────────────────────────────────────

    async def _eth_evaluate(self, window: WindowState) -> None:
        """ETH: Run strategy evaluation (T-30s)."""
        try:
            decision = await self._eth_strategy.evaluate(window)
            self._dashboard.push("eth_confidence", decision.confidence.to_dict())
            if self._eth_strategy.last_consensus:
                self._dashboard.push("eth_agent_votes", self._eth_strategy.last_consensus.to_dict())
        except Exception as exc:
            logger.error(f"ETH strategy evaluation error: {exc}", exc_info=True)

    async def _eth_decide(self, window: WindowState) -> None:
        await self._eth_maybe_trade(window, is_deadline=False)

    async def _eth_deadline_trade(self, window: WindowState) -> None:
        await self._eth_maybe_trade(window, is_deadline=True)

    async def _eth_maybe_trade(self, window: WindowState, is_deadline: bool) -> None:
        """ETH core trading decision logic."""
        breaker = self._circuit.evaluate(
            daily_loss_usd=self._pnl.get_daily_loss(),
            drawdown_pct=self._drawdown.drawdown_pct,
            consecutive_losses=self._pnl.get_consecutive_losses(),
            balance=self._balance,
        )
        if not breaker.can_trade:
            return

        can_open, reason = self._exposure.can_open_position(cfg.trade_amount_usd)
        if not can_open:
            return

        existing = self._eth_order_manager.get_active_for_window(window.window_ts)
        if existing:
            return
        attempted = [o for o in self._eth_order_manager.get_recent_history(10)
                     if o.window_ts == window.window_ts]
        if attempted:
            return

        try:
            decision = await self._eth_strategy.evaluate(window)
        except Exception as exc:
            logger.error(f"ETH strategy error: {exc}", exc_info=True)
            return

        self._dashboard.push("eth_confidence", decision.confidence.to_dict())
        if self._eth_strategy.last_consensus:
            self._dashboard.push("eth_agent_votes", self._eth_strategy.last_consensus.to_dict())

        if not decision.should_trade:
            return

        market = await self._eth_token_resolver.resolve_current()
        if not market:
            logger.warning("Could not resolve ETH market — skip")
            return

        if decision.confidence.total < 51:
            return

        stats = self._pnl.get_stats()
        win_rate = stats.win_rate if stats.total_trades >= 10 else 0.55
        token_price = (
            market.yes_price if decision.direction == "UP" else market.no_price
        )
        token_price = max(cfg.min_token_price, min(cfg.max_token_price, token_price))

        sizing = self._sizer.calculate(
            balance=self._balance,
            confidence=decision.confidence.total,
            win_rate=win_rate,
            token_price=token_price,
            consecutive_losses=self._pnl.get_consecutive_losses(),
            drawdown_pct=self._drawdown.drawdown_pct,
        )
        size_usd = sizing.size_usd * breaker.size_multiplier

        if size_usd < 1.0:
            return

        order = await self._eth_executor.execute(
            market=market,
            direction=decision.direction,
            confidence=decision.confidence.total,
            position_size_usd=size_usd,
        )

        if order:
            self._exposure.open_position(order.size_usd)
            self._balance -= (order.size_usd + order.fee_usd)
            self._drawdown.update(self._balance)
            self._eth_last_trade_votes = (
                self._eth_strategy.last_consensus.votes
                if self._eth_strategy.last_consensus else []
            )

            self._dashboard.push("eth_trade_executed", {
                "order_id": order.order_id,
                "market": order.market_slug,
                "direction": order.direction,
                "price": order.price,
                "size_usd": order.size_usd,
                "confidence": order.confidence,
                "window_ts": order.window_ts,
            })
            self._dashboard.push_log(
                "TRADE", "executor",
                f"ETH {'[PAPER] ' if order.is_paper else ''}TRADE {order.direction} "
                f"@ {order.price:.3f} × ${order.size_usd:.2f} "
                f"(conf={order.confidence:.0f})"
            )

    async def _maybe_trade(self, window: WindowState, is_deadline: bool) -> None:
        """Core trading decision logic."""
        # Check circuit breaker
        breaker = self._circuit.evaluate(
            daily_loss_usd=self._pnl.get_daily_loss(),
            drawdown_pct=self._drawdown.drawdown_pct,
            consecutive_losses=self._pnl.get_consecutive_losses(),
            balance=self._balance,
        )

        if not breaker.can_trade:
            logger.warning(
                f"Circuit breaker {breaker.tier.value}: {breaker.reason} — skip"
            )
            self._dashboard.push("circuit_breaker", breaker.to_dict())
            return

        # Check exposure limits
        can_open, reason = self._exposure.can_open_position(cfg.trade_amount_usd)
        if not can_open:
            logger.info(f"Exposure limit: {reason} — skip")
            return

        # Check for existing or already-attempted position in this window
        existing = self._order_manager.get_active_for_window(window.window_ts)
        if existing:
            logger.debug("Already have active position in this window — skip")
            return
        attempted = [o for o in self._order_manager.get_recent_history(10)
                     if o.window_ts == window.window_ts]
        if attempted:
            logger.debug("Already attempted a trade in this window — skip")
            return

        # Run strategy
        try:
            decision = await self._strategy.evaluate(window)
        except Exception as exc:
            logger.error(f"Strategy error: {exc}", exc_info=True)
            return

        self._dashboard.push("confidence", decision.confidence.to_dict())
        if self._strategy.last_consensus:
            self._dashboard.push("agent_votes", self._strategy.last_consensus.to_dict())

        if not decision.should_trade:
            if is_deadline:
                logger.info(f"Deadline skip: {decision.reason}")
            return

        # Resolve market
        market = await self._token_resolver.resolve_current()
        if not market:
            logger.warning("Could not resolve current market — skip")
            return

        # Hard floor: Kelly requires confidence > 50 for positive edge
        if decision.confidence.total < 51:
            logger.info(f"Confidence {decision.confidence.total:.0f} below Kelly floor — skip")
            return

        # Calculate position size
        stats = self._pnl.get_stats()
        win_rate = stats.win_rate if stats.total_trades >= 10 else 0.55
        token_price = (
            market.yes_price if decision.direction == "UP" else market.no_price
        )

        # Guard: Gamma prices can be stale/extreme. Clamp to a sane range
        # so the sizer never sees a price outside the executor's own limits.
        # The executor will do its own live-ask check; this just prevents the
        # Kelly from going negative on a stale market.yes_price from Gamma.
        token_price = max(cfg.min_token_price, min(cfg.max_token_price, token_price))

        sizing = self._sizer.calculate(
            balance=self._balance,
            confidence=decision.confidence.total,
            win_rate=win_rate,
            token_price=token_price,
            consecutive_losses=self._pnl.get_consecutive_losses(),
            drawdown_pct=self._drawdown.drawdown_pct,
        )

        # Apply circuit breaker size multiplier
        size_usd = sizing.size_usd * breaker.size_multiplier

        if size_usd < 1.0:
            reason = (
                f"after {breaker.tier.value} circuit breaker ×{breaker.size_multiplier}"
                if breaker.size_multiplier < 1.0
                else f"sizing returned ${sizing.size_usd:.2f} ({', '.join(sizing.adjustments)})"
            )
            logger.info(f"Position size too small — skip ({reason})")
            return

        # Execute
        order = await self._executor.execute(
            market=market,
            direction=decision.direction,
            confidence=decision.confidence.total,
            position_size_usd=size_usd,
        )

        if order:
            self._exposure.open_position(order.size_usd)
            self._balance -= (order.size_usd + order.fee_usd)
            self._drawdown.update(self._balance)
            self._last_trade_votes = (
                self._strategy.last_consensus.votes
                if self._strategy.last_consensus else []
            )

            self._dashboard.push("trade_executed", {
                "order_id": order.order_id,
                "market": order.market_slug,
                "direction": order.direction,
                "price": order.price,
                "size_usd": order.size_usd,
                "confidence": order.confidence,
                "window_ts": order.window_ts,
            })
            self._dashboard.push_log(
                "TRADE", "executor",
                f"{'[PAPER] ' if order.is_paper else ''}TRADE {order.direction} "
                f"@ {order.price:.3f} × ${order.size_usd:.2f} "
                f"(conf={order.confidence:.0f})"
            )

    # ── Resolution ────────────────────────────────────────────────────────────

    async def _process_window_resolution(self, window: WindowState) -> None:
        """Wait for market resolution and process claims."""
        # Wait for Chainlink to publish final price
        await asyncio.sleep(15)

        # In paper mode: determine outcome from window delta
        # In live mode: query the resolved token price from Polymarket REST API
        actual_direction = await self._determine_resolution(window)

        if actual_direction is None:
            logger.warning(f"Could not determine resolution for {window.window_slug}")
            return

        logger.info(f"Resolution: {window.window_slug} → {actual_direction}")
        self._dashboard.push_log(
            "INFO", "resolution",
            f"Window resolved: {window.window_slug} → {actual_direction}"
        )

        # Process orders for this window
        window_orders = [
            o for o in self._order_manager.get_recent_history(20)
            if o.window_ts == window.window_ts
        ]

        for order in window_orders:
            # Skip orders that never fully executed, but notify dashboard to remove them
            if order.status in (
                OrderStatus.CANCELLED, OrderStatus.REJECTED,
                OrderStatus.EXPIRED, OrderStatus.PENDING,
            ):
                self._dashboard.push("trade_cancelled", {"order_id": order.order_id})
                continue

            won = order.direction == actual_direction
            pnl = (
                (order.filled_shares - order.size_usd)
                if won
                else -(order.size_usd + order.fee_usd)
            )

            self._claimer.schedule_claim(order, actual_direction)
            self._pnl.record_trade(
                trade_id=order.order_id,
                direction=order.direction,
                won=won,
                pnl=pnl,
                entry_price=order.price,
                confidence=order.confidence,
                window_ts=order.window_ts,
            )

            if won:
                self._balance += order.filled_shares  # Payout at $1.00/share
                logger.trade(  # type: ignore[attr-defined]
                    f"WIN ${pnl:+.2f}: {order.direction} on {order.market_slug[:20]}..."
                )
            else:
                logger.trade(  # type: ignore[attr-defined]
                    f"LOSS ${pnl:.2f}: {order.direction} on {order.market_slug[:20]}..."
                )

            self._exposure.close_position(order.size_usd)

            self._dashboard.push("trade_resolved", {
                "order_id": order.order_id,
                "market": order.market_slug,
                "direction": order.direction,
                "actual_direction": actual_direction,
                "won": won,
                "pnl": round(pnl, 2),
                "window_ts": window.window_ts,
            })

        # Update portfolio stats
        self._drawdown.update(self._balance)
        stats = self._pnl.get_stats()
        self._dashboard.push("portfolio_update", {
            "balance": round(self._balance, 2),
            **stats.to_dict(),
        })

        # Process claims
        await self._claimer.process_pending_claims(self._wallet)

        # Update agent meta-learner
        if actual_direction and self._last_trade_votes:
            self._consensus.record_outcome(actual_direction, self._last_trade_votes)

    async def _determine_resolution(self, window: WindowState) -> str | None:
        """
        Determine the actual outcome of a window.

        Live mode: queries the Polymarket REST API for the resolved token prices.
          A resolved YES token trades at ~$1.00 (UP wins) or ~$0.00 (DOWN wins).
          This is authoritative — never infer from BTC price in live mode.

        Paper mode: infers from the window delta (BTC vs open price at close).
        """
        # ── Live mode: query actual market resolution ─────────────────────────
        if not cfg.paper_mode:
            try:
                market = await self._token_resolver.resolve_current()
                if market:
                    # Poll YES token midpoint — resolved markets go to 0.00 or 1.00
                    async with PolymarketRestClient() as rest:
                        book = await rest.get_order_book(market.yes_token_id)
                        asks = book.get("asks", [])
                        bids = book.get("bids", [])
                        # After resolution, one side collapses: price → 0 or → 1
                        if asks:
                            yes_price = float(asks[0]["price"])
                        elif bids:
                            yes_price = float(bids[0]["price"])
                        else:
                            yes_price = -1.0

                        if yes_price >= 0.95:
                            logger.info(
                                f"Live resolution: YES token @ {yes_price:.3f} → UP"
                            )
                            return "UP"
                        elif 0.0 <= yes_price <= 0.05:
                            logger.info(
                                f"Live resolution: YES token @ {yes_price:.3f} → DOWN"
                            )
                            return "DOWN"
                        else:
                            logger.warning(
                                f"Live resolution ambiguous: YES token @ {yes_price:.3f} "
                                f"— falling back to price inference"
                            )
            except Exception as exc:
                logger.warning(
                    f"Live resolution query failed: {exc} — falling back to price inference"
                )

        # ── Paper mode (or live fallback): infer from price ───────────────────
        if window.open_price <= 0:
            return None

        current = self._aggregator.current_price
        if current <= 0:
            return None

        # Prefer Chainlink oracle (same source Polymarket uses) over CEX price
        oracle = self._oracle.latest
        if oracle and oracle.price > 0:
            if oracle.price > window.open_price:
                return "UP"
            elif oracle.price < window.open_price:
                return "DOWN"

        if current >= window.open_price:
            return "UP"
        return "DOWN"

    async def _eth_process_window_resolution(self, window: WindowState) -> None:
        """ETH: Wait for market resolution and process ETH claims."""
        await asyncio.sleep(15)

        actual_direction = await self._eth_determine_resolution(window)
        if actual_direction is None:
            logger.warning(f"Could not determine ETH resolution for {window.window_slug}")
            return

        logger.info(f"ETH Resolution: {window.window_slug} → {actual_direction}")

        window_orders = [
            o for o in self._eth_order_manager.get_recent_history(20)
            if o.window_ts == window.window_ts
        ]

        for order in window_orders:
            if order.status in (
                OrderStatus.CANCELLED, OrderStatus.REJECTED,
                OrderStatus.EXPIRED, OrderStatus.PENDING,
            ):
                self._dashboard.push("eth_trade_cancelled", {"order_id": order.order_id})
                continue

            won = order.direction == actual_direction
            pnl = (
                (order.filled_shares - order.size_usd)
                if won
                else -(order.size_usd + order.fee_usd)
            )

            self._eth_claimer.schedule_claim(order, actual_direction)
            self._pnl.record_trade(
                trade_id=order.order_id,
                direction=order.direction,
                won=won,
                pnl=pnl,
                entry_price=order.price,
                confidence=order.confidence,
                window_ts=order.window_ts,
            )

            if won:
                self._balance += order.filled_shares
            self._exposure.close_position(order.size_usd)

            self._dashboard.push("eth_trade_resolved", {
                "order_id": order.order_id,
                "market": order.market_slug,
                "direction": order.direction,
                "actual_direction": actual_direction,
                "won": won,
                "pnl": round(pnl, 2),
                "window_ts": window.window_ts,
            })

        self._drawdown.update(self._balance)
        await self._eth_claimer.process_pending_claims(self._wallet)

        if actual_direction and self._eth_last_trade_votes:
            self._eth_consensus.record_outcome(actual_direction, self._eth_last_trade_votes)

    async def _eth_determine_resolution(self, window: WindowState) -> str | None:
        """Determine ETH window outcome (paper: from ETH price delta)."""
        if not cfg.paper_mode:
            try:
                market = await self._eth_token_resolver.resolve_current()
                if market:
                    async with PolymarketRestClient() as rest:
                        book = await rest.get_order_book(market.yes_token_id)
                        asks = book.get("asks", [])
                        bids = book.get("bids", [])
                        if asks:
                            yes_price = float(asks[0]["price"])
                        elif bids:
                            yes_price = float(bids[0]["price"])
                        else:
                            yes_price = -1.0

                        if yes_price >= 0.95:
                            return "UP"
                        elif 0.0 <= yes_price <= 0.05:
                            return "DOWN"
            except Exception as exc:
                logger.warning(f"ETH live resolution failed: {exc}")

        eth_open = self._eth_strategy.window_open_price
        if eth_open <= 0:
            return None

        oracle = self._eth_oracle.latest
        if oracle and oracle.price > 0:
            return "UP" if oracle.price > eth_open else "DOWN"

        current = self._eth_aggregator.current_price
        if current <= 0:
            return None
        return "UP" if current >= eth_open else "DOWN"

    # ── Market subscription ───────────────────────────────────────────────────

    async def _subscribe_market(self, window: WindowState) -> None:
        """Subscribe to order book for the current BTC window's market."""
        try:
            market = await self._token_resolver.resolve_current()
            if market:
                self._poly_ws.subscribe_token(market.yes_token_id)
                self._poly_ws.subscribe_token(market.no_token_id)
                self._strategy.set_current_market_tokens(market.yes_token_id, market.no_token_id)
                logger.debug(f"Subscribed to BTC order books for {market.slug}")
        except Exception as exc:
            logger.warning(f"Could not subscribe to BTC market order book: {exc}")

    async def _eth_subscribe_market(self, window: WindowState) -> None:
        """Subscribe to order book for the current ETH window's market."""
        try:
            market = await self._eth_token_resolver.resolve_current()
            if market:
                self._poly_ws.subscribe_token(market.yes_token_id)
                self._poly_ws.subscribe_token(market.no_token_id)
                self._eth_strategy.set_current_market_tokens(market.yes_token_id, market.no_token_id)
                logger.debug(f"Subscribed to ETH order books for {market.slug}")
        except Exception as exc:
            logger.warning(f"Could not subscribe to ETH market order book: {exc}")

    # ── Heartbeat ─────────────────────────────────────────────────────────────

    async def _heartbeat_loop(self) -> None:
        """Periodic health check and portfolio update push."""
        while self._running:
            await asyncio.sleep(30)

            # Update balance from chain if live
            if self._wallet and not cfg.paper_mode:
                try:
                    usdc = await self._wallet.get_usdc_balance()
                    if usdc > 0:
                        self._balance = usdc
                except Exception as exc:
                    logger.warning(f"Balance refresh failed: {exc}")

            stats = self._pnl.get_stats()
            self._dashboard.push("portfolio_update", {
                "balance": round(self._balance, 2),
                **stats.to_dict(),
            })

            breaker = self._circuit.status
            self._dashboard.push("circuit_breaker", breaker.to_dict())

    # ── Command handler ───────────────────────────────────────────────────────

    def _push_updated_agent_votes(self) -> None:
        """Re-apply meta-learner weights to last consensus and push to dashboard."""
        if not self._strategy.last_consensus:
            return
        self._meta_learner.apply_to_votes(self._strategy.last_consensus.votes)
        self._dashboard.push("agent_votes", self._strategy.last_consensus.to_dict())

    async def _handle_command(self, cmd: dict) -> None:
        """Handle commands from the dashboard."""
        cmd_type = cmd.get("command", "")

        if cmd_type == "pause":
            self._circuit.manual_pause()
            logger.info("Bot paused via dashboard command")

        elif cmd_type == "resume":
            self._circuit.manual_resume()
            logger.info("Bot resumed via dashboard command")

        elif cmd_type == "emergency_stop":
            self._circuit.trigger_emergency_stop("Dashboard emergency stop")
            logger.critical("EMERGENCY STOP triggered from dashboard")

        elif cmd_type == "status":
            stats = self._pnl.get_stats()
            self._dashboard.push("portfolio_update", {
                "balance": round(self._balance, 2),
                **stats.to_dict(),
            })

        elif cmd_type == "set_confidence":
            new_conf = int(cmd.get("value", cfg.min_confidence_score))
            cfg.min_confidence_score = max(0, min(100, new_conf))
            logger.info(f"Confidence threshold updated to {cfg.min_confidence_score}")

        elif cmd_type == "unmute_agent":
            agent_name = cmd.get("agent", "")
            if self._meta_learner.force_unmute(agent_name):
                logger.info(f"Agent '{agent_name}' unmuted via dashboard")
                self._push_updated_agent_votes()
            else:
                logger.warning(f"Unmute failed: agent '{agent_name}' not found")

        elif cmd_type == "mute_agent":
            agent_name = cmd.get("agent", "")
            if self._meta_learner.force_mute(agent_name):
                logger.info(f"Agent '{agent_name}' muted via dashboard")
                self._push_updated_agent_votes()
            else:
                logger.warning(f"Mute failed: agent '{agent_name}' not found")

        else:
            logger.warning(f"Unknown command: {cmd_type}")


# ── CLI entry point ───────────────────────────────────────────────────────────

@click.command()
@click.option("--paper/--live", default=None, help="Override paper/live mode from .env")
@click.option("--log-level", default=None, help="Override log level")
def cli_main(paper: bool | None, log_level: str | None) -> None:
    """PolyOracle — Autonomous Polymarket BTC prediction market bot."""
    # Setup logging
    level = log_level or cfg.log_level
    setup_logging(level=level, log_file=cfg.log_file)

    # Override paper mode if specified
    if paper is not None:
        import os
        os.environ["PAPER_MODE"] = "true" if paper else "false"

    logger.info(
        f"PolyOracle starting | "
        f"mode={'PAPER' if cfg.paper_mode else 'LIVE'} | "
        f"confidence_threshold={cfg.min_confidence_score}"
    )

    bot = PolyOracle(paper_mode=paper)

    # Handle Ctrl+C gracefully
    loop = asyncio.get_event_loop()

    def _shutdown_handler():
        logger.info("Shutdown signal received")
        for task in asyncio.all_tasks(loop):
            task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown_handler)

    try:
        loop.run_until_complete(bot.run())
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()
        logger.info("PolyOracle stopped")


if __name__ == "__main__":
    cli_main()
