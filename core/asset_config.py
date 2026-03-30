"""
Asset configuration — defines per-asset settings and the default registry.

Each tradable asset (BTC, ETH, SOL, …) is described by an AssetConfig that
holds everything needed to wire up a trading lane: Binance feed URL,
Chainlink oracle proxy, Polymarket slug prefix, and CLOB search keywords.

Adding a new asset = adding one entry to DEFAULT_ASSETS (or overriding via
the ASSETS_JSON env var).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class AssetConfig(BaseModel):
    """Configuration for a single tradable asset."""

    model_config = ConfigDict(frozen=True)

    symbol: str = Field(description="Uppercase ticker: BTC, ETH, SOL, …")
    binance_symbol: str = Field(description="Binance spot pair: BTCUSDT, …")
    binance_ws_url: str = Field(description="Binance trade stream WebSocket URL")
    chainlink_proxy: str = Field(description="Chainlink price feed proxy on Polygon")
    slug_prefix: str = Field(description="Polymarket slug prefix: btc, eth, sol, …")
    clob_keywords: list[str] = Field(
        default_factory=list,
        description="Fallback search terms for CLOB market resolution",
    )
    enabled: bool = True


def _build_binance_ws_url(symbol: str) -> str:
    """Construct a Binance trade-stream URL from a spot pair symbol."""
    return f"wss://stream.binance.com:9443/ws/{symbol.lower()}@trade"


# ── Default asset registry ────────────────────────────────────────────────────
# These are the assets the bot trades out of the box.  Override or extend via
# the ASSETS_JSON environment variable (JSON list of partial AssetConfig dicts).

DEFAULT_ASSETS: list[AssetConfig] = [
    AssetConfig(
        symbol="BTC",
        binance_symbol="BTCUSDT",
        binance_ws_url=_build_binance_ws_url("BTCUSDT"),
        chainlink_proxy="0xc907E116054Ad103354f2D350FD2514433D57F6f",
        slug_prefix="btc",
        clob_keywords=["btc", "bitcoin"],
    ),
    AssetConfig(
        symbol="ETH",
        binance_symbol="ETHUSDT",
        binance_ws_url=_build_binance_ws_url("ETHUSDT"),
        chainlink_proxy="0xF9680D99D6C9589e2a93a78A04A279e509205945",
        slug_prefix="eth",
        clob_keywords=["eth", "ethereum"],
    ),
    AssetConfig(
        symbol="SOL",
        binance_symbol="SOLUSDT",
        binance_ws_url=_build_binance_ws_url("SOLUSDT"),
        chainlink_proxy="0x4F6C2860e2B3a5CfC3BaC5cF44EB3F09dD0b738",
        slug_prefix="sol",
        clob_keywords=["sol", "solana"],
    ),
    AssetConfig(
        symbol="DOGE",
        binance_symbol="DOGEUSDT",
        binance_ws_url=_build_binance_ws_url("DOGEUSDT"),
        # Chainlink DOGE/USD feed on Polygon was deprecated — disabled until a
        # replacement proxy is confirmed.
        chainlink_proxy="0xbaf9327b6564454F4a3364C33eFeEf032b4b4444",
        slug_prefix="doge",
        clob_keywords=["doge", "dogecoin"],
        enabled=False,
    ),
    AssetConfig(
        symbol="XRP",
        binance_symbol="XRPUSDT",
        binance_ws_url=_build_binance_ws_url("XRPUSDT"),
        chainlink_proxy="0x785ba89291f676b5386652eB12b30cF361020694",
        slug_prefix="xrp",
        clob_keywords=["xrp", "ripple"],
    ),
]
