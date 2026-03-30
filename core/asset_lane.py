"""
AssetLane — per-asset runtime bundle.

Each enabled asset (BTC, ETH, SOL, …) gets its own AssetLane that holds
every runtime object needed for independent trading: data feeds, agents,
strategy, executor, order manager, and claimer.

Shared objects (risk stack, PolymarketWebSocket, wallet, fee calculator)
live on PolyOracle and are passed in at creation time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agents.consensus import ConsensusEngine
from agents.meta_learner import MetaLearner
from agents.momentum_agent import MomentumAgent
from agents.mean_reversion_agent import MeanReversionAgent
from agents.volatility_agent import VolatilityAgent
from agents.orderflow_agent import OrderFlowAgent
from agents.oracle_agent import OracleAgent
from core.asset_config import AssetConfig
from data.aggregator import PriceAggregator
from data.binance_ws import BinanceWebSocket
from data.candle_builder import CandleBuilder
from data.chainlink_oracle import ChainlinkOracle
from execution.claimer import Claimer
from execution.order_manager import OrderManager
from execution.polymarket_executor import PolymarketExecutor
from execution.token_resolver import TokenResolver
from strategy.late_window import LateWindowStrategy

if TYPE_CHECKING:
    from data.gamma_api import GammaClient
    from data.polymarket_rest import PolymarketRestClient
    from data.polymarket_ws import PolymarketWebSocket
    from execution.fee_calculator import FeeCalculator
    from execution.wallet import Wallet


@dataclass
class AssetLane:
    """All runtime objects for one tradable asset."""

    config: AssetConfig
    aggregator: PriceAggregator
    candles: CandleBuilder
    binance_ws: BinanceWebSocket
    oracle: ChainlinkOracle
    token_resolver: TokenResolver
    meta_learner: MetaLearner
    agents: list
    consensus: ConsensusEngine
    strategy: LateWindowStrategy
    order_manager: OrderManager
    executor: PolymarketExecutor | None
    claimer: Claimer

    # Per-window mutable state
    last_trade_votes: list = field(default_factory=list)
    last_eval_tick: float = 0.0

    @classmethod
    def create(
        cls,
        config: AssetConfig,
        poly_ws: PolymarketWebSocket,
        gamma: GammaClient,
        rest_client: PolymarketRestClient,
        wallet: Wallet | None,
        fee_calc: FeeCalculator,
    ) -> AssetLane:
        """Wire up all per-asset components from a single config entry."""
        aggregator = PriceAggregator()
        candles = CandleBuilder()
        binance_ws = BinanceWebSocket(config.binance_ws_url)
        oracle = ChainlinkOracle(config.chainlink_proxy)
        token_resolver = TokenResolver(
            gamma, rest_client,
            asset=config.slug_prefix,
            clob_keywords=config.clob_keywords,
        )

        meta_learner = MetaLearner()
        agents = [
            MomentumAgent(),
            MeanReversionAgent(),
            VolatilityAgent(),
            OrderFlowAgent(),
            OracleAgent(),
        ]
        consensus = ConsensusEngine(agents, meta_learner)

        strategy = LateWindowStrategy(
            candle_builder=candles,
            aggregator=aggregator,
            poly_ws=poly_ws,
            oracle=oracle,
            consensus_engine=consensus,
        )

        order_manager = OrderManager()
        claimer = Claimer(order_manager)

        executor = PolymarketExecutor(
            wallet=wallet,  # type: ignore[arg-type]
            order_manager=order_manager,
            fee_calculator=fee_calc,
        )

        return cls(
            config=config,
            aggregator=aggregator,
            candles=candles,
            binance_ws=binance_ws,
            oracle=oracle,
            token_resolver=token_resolver,
            meta_learner=meta_learner,
            agents=agents,
            consensus=consensus,
            strategy=strategy,
            order_manager=order_manager,
            executor=executor,
            claimer=claimer,
        )
