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
            asset                TEXT    NOT NULL,
            market               TEXT    NOT NULL,
            direction            TEXT    NOT NULL,          -- 'UP' or 'DOWN'
            entry_price          REAL    NOT NULL,
            size_usd             REAL    NOT NULL,
            fee_usd              REAL,                      -- fee paid at execution
            confidence           REAL    NOT NULL,
            window_ts            INTEGER NOT NULL,
            order_type           TEXT    NOT NULL,          -- 'GTC' or 'FOK'
            entry_sec_remaining  REAL,                      -- seconds left in window at entry
            window_delta_pct     REAL,
            open_price           REAL,                      -- asset price at window open
            agent_votes          TEXT,                      -- JSON array
            confidence_breakdown TEXT,                      -- JSON object
            created_at           REAL    NOT NULL,
            -- Filled in after resolution:
            won                  INTEGER,                   -- NULL until resolved
            actual_direction     TEXT,
            pnl                  REAL,
            filled_shares        REAL,                      -- actual shares received
            filled_price         REAL,                      -- actual fill price
            close_price          REAL,                      -- asset price at window close
            price_move_pct       REAL,                      -- % move open→close
            resolution_method    TEXT,                      -- 'oracle'/'clob'/'binance'/'paper'
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


def _migrate(conn: sqlite3.Connection) -> None:
    """Add any columns introduced after the initial schema."""
    migrations = [
        ("open_price",          "ALTER TABLE trades ADD COLUMN open_price REAL"),
        ("fee_usd",             "ALTER TABLE trades ADD COLUMN fee_usd REAL"),
        ("entry_sec_remaining", "ALTER TABLE trades ADD COLUMN entry_sec_remaining REAL"),
        ("filled_shares",       "ALTER TABLE trades ADD COLUMN filled_shares REAL"),
        ("filled_price",        "ALTER TABLE trades ADD COLUMN filled_price REAL"),
        ("close_price",         "ALTER TABLE trades ADD COLUMN close_price REAL"),
        ("price_move_pct",      "ALTER TABLE trades ADD COLUMN price_move_pct REAL"),
        ("resolution_method",   "ALTER TABLE trades ADD COLUMN resolution_method TEXT"),
    ]
    added = []
    for col, sql in migrations:
        try:
            conn.execute(sql)
            added.append(col)
        except sqlite3.OperationalError:
            pass  # Column already exists
    if added:
        conn.commit()
        logger.info(f"trade_db: migrated — added columns: {', '.join(added)}")


def _init_db() -> None:
    conn = _get_conn()
    try:
        _create_schema(conn)
        _migrate(conn)
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
    fee_usd: float | None,
    confidence: float,
    window_ts: int,
    order_type: str,
    entry_sec_remaining: float | None,
    window_delta_pct: float | None,
    open_price: float | None,
    agent_votes: list | None,
    confidence_breakdown: dict | None,
) -> None:
    conn = _get_conn()
    try:
        conn.execute("""
            INSERT OR IGNORE INTO trades
            (order_id, asset, market, direction, entry_price, size_usd, fee_usd,
             confidence, window_ts, order_type, entry_sec_remaining,
             window_delta_pct, open_price, agent_votes, confidence_breakdown, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            order_id, asset, market, direction, entry_price, size_usd, fee_usd,
            confidence, window_ts, order_type, entry_sec_remaining,
            window_delta_pct, open_price,
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
    filled_shares: float | None,
    filled_price: float | None,
    close_price: float | None,
    price_move_pct: float | None,
    resolution_method: str | None,
) -> None:
    conn = _get_conn()
    try:
        conn.execute("""
            UPDATE trades
            SET won = ?, actual_direction = ?, pnl = ?,
                filled_shares = ?, filled_price = ?,
                close_price = ?, price_move_pct = ?,
                resolution_method = ?, resolved_at = ?
            WHERE order_id = ?
        """, (
            int(won), actual_direction, pnl,
            filled_shares, filled_price,
            close_price, price_move_pct,
            resolution_method, time.time(),
            order_id,
        ))
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
    fee_usd: float | None = None,
    entry_sec_remaining: float | None = None,
    window_delta_pct: float | None = None,
    open_price: float | None = None,
    agent_votes: list | None = None,
    confidence_breakdown: dict | None = None,
) -> None:
    """Persist a new trade execution record."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        _executor, _record_trade_sync,
        order_id, asset, market, direction, entry_price, size_usd, fee_usd,
        confidence, window_ts, order_type, entry_sec_remaining,
        window_delta_pct, open_price, agent_votes, confidence_breakdown,
    )


def _update_trade_fill_sync(order_id: str, filled_shares: float, filled_price: float) -> None:
    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE trades SET filled_shares = ?, filled_price = ? WHERE order_id = ?",
            (filled_shares, filled_price, order_id),
        )
        conn.commit()
    except Exception as exc:
        logger.error(f"trade_db update_trade_fill failed: {exc}")
    finally:
        conn.close()


async def update_trade_fill(order_id: str, filled_shares: float, filled_price: float) -> None:
    """Persist fill data as soon as a GTC order fills (before resolution)."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(_executor, _update_trade_fill_sync, order_id, filled_shares, filled_price)


async def resolve_trade(
    order_id: str,
    won: bool,
    actual_direction: str,
    pnl: float,
    filled_shares: float | None = None,
    filled_price: float | None = None,
    close_price: float | None = None,
    price_move_pct: float | None = None,
    resolution_method: str | None = None,
) -> None:
    """Update a trade record with the final outcome."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        _executor, _resolve_trade_sync,
        order_id, won, actual_direction, pnl,
        filled_shares, filled_price, close_price, price_move_pct, resolution_method,
    )


# ── Query helpers ─────────────────────────────────────────────────────────────

def _load_resolved_trades_sync(asset: str | None, limit: int) -> list[dict]:
    """Load resolved trades (those with an outcome) ordered newest-first."""
    conn = _get_conn()
    try:
        query = """
            SELECT order_id, asset, market, direction, actual_direction,
                   agent_votes, confidence_breakdown, confidence, window_ts, won, pnl,
                   entry_price, size_usd, fee_usd, order_type,
                   entry_sec_remaining, window_delta_pct, open_price,
                   filled_shares, filled_price, close_price, price_move_pct,
                   resolution_method
            FROM trades
            WHERE actual_direction IS NOT NULL
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


def _load_pnl_records_sync(asset: str | None, limit: int) -> list[dict]:
    """Load resolved trade records for PnL tracker seeding (no agent_votes filter)."""
    conn = _get_conn()
    try:
        query = """
            SELECT order_id, direction, won, pnl, entry_price, confidence, window_ts
            FROM trades
            WHERE won IS NOT NULL
        """
        params: list = []
        if asset:
            query += " AND lower(asset) = lower(?)"
            params.append(asset)
        query += " ORDER BY window_ts ASC LIMIT ?"
        params.append(limit)
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


async def load_pnl_records(
    asset: str | None = None,
    limit: int = 1000,
) -> list[dict]:
    """Return resolved trade records for PnL tracker seeding (ordered oldest-first)."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        _executor, _load_pnl_records_sync, asset, limit
    )


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


def _load_unresolved_trades_sync(min_age_sec: int) -> list[dict]:
    """
    Return trades that have no outcome yet and are older than min_age_sec seconds.

    These are candidates for re-resolution on startup — the bot was stopped
    while the resolution coroutine was mid-poll.
    """
    conn = _get_conn()
    try:
        cutoff = time.time() - min_age_sec
        rows = conn.execute("""
            SELECT order_id, asset, market, window_ts, direction,
                   open_price, entry_price, size_usd, fee_usd, confidence,
                   filled_shares, filled_price,
                   agent_votes, confidence_breakdown, window_delta_pct
            FROM trades
            WHERE won IS NULL
              AND created_at < ?
            ORDER BY window_ts ASC
        """, (cutoff,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


async def load_unresolved_trades(min_age_sec: int = 600) -> list[dict]:
    """
    Return trades with no outcome that are at least min_age_sec seconds old.
    Used at startup to re-resolve any trades that survived a bot restart.
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, _load_unresolved_trades_sync, min_age_sec)


# ── Init on import ────────────────────────────────────────────────────────────

try:
    _init_db()
    logger.info(f"Trade DB initialised: {_DB_PATH.resolve()}")
except Exception as exc:
    logger.error(f"Trade DB init failed: {exc}")
