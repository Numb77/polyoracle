"""
WebSocket server — pushes real-time bot state to the Next.js dashboard.

The bot writes state to this server via an asyncio Queue.
Connected dashboard clients receive JSON messages on every state update.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import websockets
from websockets.server import WebSocketServerProtocol
from websockets.exceptions import ConnectionClosed

from core.config import get_config
from core.logger import get_logger

logger = get_logger(__name__)
cfg = get_config()


class DashboardServer:
    """
    asyncio WebSocket server that pushes state updates to all connected
    dashboard clients.

    Message types sent to clients:
        tick              — BTC price update (1s)
        window_state      — Current window phase and metrics
        agent_votes       — Agent consensus result
        confidence        — Confidence score breakdown
        trade_executed    — Trade was placed
        trade_resolved    — Trade result (win/loss)
        circuit_breaker   — Risk alert
        portfolio_update  — Balance and statistics
        log               — Log message for terminal tab
    """

    def __init__(self) -> None:
        self._clients: set[WebSocketServerProtocol] = set()
        self._queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=1000)
        self._running = False

        # Latest state snapshot (for new client catch-up)
        self._last_state: dict[str, Any] = {}

        # Rolling history pushed to new clients on connect
        # Keeps the last 500 resolved trade events (BTC + ETH combined)
        self._trade_history: list[dict] = []
        # Keeps the last 300 log messages
        self._log_buffer: list[dict] = []

    # ── State push API (called by bot) ────────────────────────────────────────

    def push(self, msg_type: str, data: Any) -> None:
        """Enqueue a message to be sent to all connected clients."""
        msg = {"type": msg_type, "data": data, "ts": time.time()}
        try:
            self._queue.put_nowait(msg)
            # Cache latest state per type for new clients
            # Cache per (type, asset) so each asset's latest state is preserved
            if isinstance(data, dict) and "asset" in data:
                cache_key = f"{msg_type}:{data['asset']}"
            else:
                cache_key = msg_type
            self._last_state[cache_key] = msg
            # Track trade history for catch-up on new connect
            if msg_type == "trade_resolved":
                self._trade_history.append(msg)
                if len(self._trade_history) > 500:
                    self._trade_history = self._trade_history[-500:]
        except asyncio.QueueFull:
            # Drop oldest message to make room
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(msg)
            except Exception:
                pass

    def push_log(self, level: str, module: str, message: str) -> None:
        """Push a log message to the terminal tab."""
        data = {
            "level": level,
            "module": module,
            "message": message,
            "timestamp": time.strftime("%H:%M:%S"),
        }
        msg = {"type": "log", "data": data, "ts": time.time()}
        self._log_buffer.append(msg)
        if len(self._log_buffer) > 300:
            self._log_buffer = self._log_buffer[-300:]
        try:
            self._queue.put_nowait(msg)
            self._last_state["log"] = msg
        except asyncio.QueueFull:
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(msg)
            except Exception:
                pass

    # ── Server lifecycle ──────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the WebSocket server and the broadcast loop."""
        import logging as _logging
        # Suppress noisy "opening handshake failed" errors that fire when browsers
        # or health-check probes connect to the WS port without doing a proper
        # WebSocket upgrade (e.g. Next.js HMR, macOS mDNS probes). These are
        # harmless connection resets and don't need to appear as ERRORs.
        _logging.getLogger("websockets.server").setLevel(_logging.CRITICAL)

        self._running = True
        host = "localhost"
        port = cfg.ws_server_port

        logger.info(f"Dashboard WebSocket server starting on ws://{host}:{port}")

        async with websockets.serve(
            self._handle_client,
            host,
            port,
            ping_interval=20,
            ping_timeout=10,
        ):
            logger.info(f"Dashboard WebSocket server listening on ws://{host}:{port}")
            await self._broadcast_loop()

    async def _broadcast_loop(self) -> None:
        """Dequeue messages and broadcast to all connected clients."""
        while self._running:
            try:
                msg = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                if self._clients:
                    payload = json.dumps(msg, default=str)
                    dead_clients = set()
                    for client in self._clients:
                        try:
                            await client.send(payload)
                        except ConnectionClosed:
                            dead_clients.add(client)
                        except Exception as exc:
                            logger.warning(f"Send error to client: {exc}")
                            dead_clients.add(client)
                    self._clients -= dead_clients
            except asyncio.TimeoutError:
                pass
            except Exception as exc:
                logger.error(f"Broadcast loop error: {exc}", exc_info=True)

    async def _handle_client(
        self, websocket: WebSocketServerProtocol
    ) -> None:
        """Handle a new dashboard client connection."""
        client_addr = websocket.remote_address
        logger.info(f"Dashboard client connected: {client_addr}")
        self._clients.add(websocket)

        # Tell the frontend to clear its log/trade buffers before we replay them.
        # Without this, every reconnect (e.g. Next.js hot reload) doubles the
        # log and trade history because the buffer is replayed on top of existing state.
        try:
            await websocket.send(json.dumps(
                {"type": "connection_init", "data": {"reset": True}, "ts": time.time()},
                default=str,
            ))
        except Exception:
            pass

        # Send catch-up snapshot of latest state (window, agents, confidence, etc.)
        for msg_type, msg in self._last_state.items():
            if msg_type == "log":
                continue  # Sent separately below
            try:
                await websocket.send(json.dumps(msg, default=str))
            except Exception:
                pass

        # Replay recent log buffer so the terminal shows history from before connect
        for log_msg in self._log_buffer[-200:]:
            try:
                await websocket.send(json.dumps(log_msg, default=str))
            except Exception:
                break

        # Replay resolved trade history so Trade History tab is populated immediately
        for trade_msg in self._trade_history:
            try:
                await websocket.send(json.dumps(trade_msg, default=str))
            except Exception:
                break

        try:
            # Handle incoming commands from dashboard
            async for raw_msg in websocket:
                await self._handle_command(raw_msg, websocket)
        except ConnectionClosed:
            pass
        except Exception as exc:
            logger.warning(f"Client {client_addr} error: {exc}")
        finally:
            self._clients.discard(websocket)
            logger.info(f"Dashboard client disconnected: {client_addr}")

    async def _handle_command(
        self, raw_msg: str, websocket: WebSocketServerProtocol
    ) -> None:
        """Handle commands sent from the dashboard."""
        try:
            cmd = json.loads(raw_msg)
            cmd_type = cmd.get("command", cmd.get("type", ""))
            logger.info(f"Dashboard command received: {cmd_type}")

            # Commands are forwarded to the bot via a separate command queue
            # that core/main.py reads.
            if hasattr(self, "_command_handler") and self._command_handler:
                await self._command_handler(cmd)

        except json.JSONDecodeError:
            logger.warning(f"Invalid command JSON: {raw_msg[:100]}")
        except Exception as exc:
            logger.error(f"Command handling error: {exc}")

    def set_command_handler(self, handler) -> None:
        """Register a handler for incoming dashboard commands."""
        self._command_handler = handler

    def stop(self) -> None:
        self._running = False

    @property
    def connected_clients(self) -> int:
        return len(self._clients)
