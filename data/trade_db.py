"""
Trade database — SQLite persistence for all executed trades.

Stores the full trade context (agent votes, confidence breakdown, entry timing,
outcome) so the algo can be improved offline based on historical performance.

Schema is intentionally flat/denormalized for easy CSV export and pandas analysis.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from core.logger import get_logger

logger = get_logger(__name__)

_DB_PATH = Path("logs/trades.db")
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="trade_db")


def _get_conn() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id             TEXT    UNIQUE NOT NULL,
            asset                TEXT    NOT NULL,          -- 'BTC' or 'ETH'
            market               TEXT    NOT NULL,          -- full slug
            direction            TEXT    NOT NULL,          -- 'UP' or 'DOWN'
            entry_price          REAL    NOT NULL,
            size_usd             REAL    NOT NULL,
            confidence           REAL    NOT NULL,
            window_ts            INTEGER NOT NULL,
            order_type           TEXT    NOT NULL,          -- 'GTC' or 'FOK'
            window_delta_pct     REAL,
            agent_votes          TEXT,                      -- JSON array
            confidence_breakdown TEXT,                      -- JSON object
            created_at           REAL    NOT NULL,
            -- Filled in after resolution:
            won                  INTEGER,                   -- NULL until resolved
            actual_direction     TEXT,
            pnl                  REAL,
            resolved_at          REAL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_trades_window_ts ON trades(window_ts)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_trades_asset ON trades(asset)
    """)
    conn.commit()


def _init_db() -> None:
    conn = _get_conn()
    try:
        _create_schema(conn)
    finally:
        conn.close()


# ── Sync helpers (run via executor) ───────────────────────────────────────────

def _record_trade_sync(
    order_id: str,
    asset: str,
    market: str,
    direction: str,
    entry_price: float,
    size_usd: float,
    confidence: float,
    window_ts: int,
    order_type: str,
    window_delta_pct: float | None,
    agent_votes: list | None,
    confidence_breakdown: dict | None,
) -> None:
    conn = _get_conn()
    try:
        conn.execute("""
            INSERT OR IGNORE INTO trades
            (order_id, asset, market, direction, entry_price, size_usd, confidence,
             window_ts, order_type, window_delta_pct, agent_votes, confidence_breakdown,
             created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            order_id, asset, market, direction, entry_price, size_usd, confidence,
            window_ts, order_type, window_delta_pct,
            json.dumps(agent_votes) if agent_votes else None,
            json.dumps(confidence_breakdown) if confidence_breakdown else None,
            time.time(),
        ))
        conn.commit()
    except Exception as exc:
        logger.error(f"trade_db record_trade failed: {exc}")
    finally:
        conn.close()


def _resolve_trade_sync(
    order_id: str,
    won: bool,
    actual_direction: str,
    pnl: float,
) -> None:
    conn = _get_conn()
    try:
        conn.execute("""
            UPDATE trades
            SET won = ?, actual_direction = ?, pnl = ?, resolved_at = ?
            WHERE order_id = ?
        """, (int(won), actual_direction, pnl, time.time(), order_id))
        conn.commit()
    except Exception as exc:
        logger.error(f"trade_db resolve_trade failed: {exc}")
    finally:
        conn.close()


# ── Async public API ──────────────────────────────────────────────────────────

async def record_trade(
    order_id: str,
    asset: str,
    market: str,
    direction: str,
    entry_price: float,
    size_usd: float,
    confidence: float,
    window_ts: int,
    order_type: str,
    window_delta_pct: float | None = None,
    agent_votes: list | None = None,
    confidence_breakdown: dict | None = None,
) -> None:
    """Persist a new trade execution record."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        _executor, _record_trade_sync,
        order_id, asset, market, direction, entry_price, size_usd, confidence,
        window_ts, order_type, window_delta_pct, agent_votes, confidence_breakdown,
    )


async def resolve_trade(
    order_id: str,
    won: bool,
    actual_direction: str,
    pnl: float,
) -> None:
    """Update a trade record with the final outcome."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        _executor, _resolve_trade_sync,
        order_id, won, actual_direction, pnl,
    )


# ── Query helpers ─────────────────────────────────────────────────────────────

def _load_resolved_trades_sync(asset: str | None, limit: int) -> list[dict]:
    """Load resolved trades (those with an outcome) ordered newest-first."""
    conn = _get_conn()
    try:
        query = """
            SELECT order_id, asset, direction, actual_direction,
                   agent_votes, confidence, window_ts, won, pnl
            FROM trades
            WHERE actual_direction IS NOT NULL
              AND agent_votes IS NOT NULL
        """
        params: list = []
        if asset:
            query += " AND lower(asset) = lower(?)"
            params.append(asset)
        query += " ORDER BY window_ts DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


async def load_resolved_trades(
    asset: str | None = None,
    limit: int = 500,
) -> list[dict]:
    """
    Return up to `limit` resolved trades (newest first) for the given asset.
    Each row has: order_id, asset, direction, actual_direction,
                  agent_votes (JSON str), confidence, window_ts, won, pnl.
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        _executor, _load_resolved_trades_sync, asset, limit
    )


# ── Init on import ────────────────────────────────────────────────────────────

try:
    _init_db()
    logger.info(f"Trade DB initialised: {_DB_PATH.resolve()}")
except Exception as exc:
    logger.error(f"Trade DB init failed: {exc}")
