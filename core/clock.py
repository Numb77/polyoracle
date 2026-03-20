"""
5-minute window timing engine.

Each Polymarket BTC Up/Down market corresponds to a 5-minute window.
Windows are aligned to Unix epoch boundaries (multiples of 300 seconds).
This module tracks which window we're in, how much time remains, and
what phase of the trading cycle we're in.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Callable, Awaitable

from core.logger import get_logger

logger = get_logger(__name__)


class WindowPhase(Enum):
    """The current phase of a 5-minute trading window."""
    MONITORING = auto()    # T+0s to T-30s  — passive observation
    EVALUATING = auto()    # T-30s to T-10s — agent analysis
    TRADING = auto()       # T-10s to T-5s  — execute if confident
    DEADLINE = auto()      # T-5s to T-0s   — hard deadline, fire or skip
    RESOLVED = auto()      # T+0s           — window closed, await resolution


@dataclass
class WindowState:
    """Snapshot of the current 5-minute window state."""
    window_ts: int          # Unix timestamp of window open (multiple of 300)
    open_price: float       # BTC price at window open (from Chainlink or first tick)
    current_price: float    # Latest BTC price
    phase: WindowPhase
    elapsed_sec: float      # Seconds since window opened
    remaining_sec: float    # Seconds until window closes

    @property
    def delta_pct(self) -> float:
        """Price change from window open, as a percentage."""
        if self.open_price <= 0:
            return 0.0
        return (self.current_price - self.open_price) / self.open_price * 100

    @property
    def window_slug(self) -> str:
        """Deterministic market slug for this window."""
        return f"btc-updown-5m-{self.window_ts}"

    @property
    def next_window_ts(self) -> int:
        """Timestamp of the next window."""
        return self.window_ts + 300

    def to_dict(self) -> dict:
        return {
            "window_ts": self.window_ts,
            "open_price": self.open_price,
            "current_price": self.current_price,
            "delta_pct": round(self.delta_pct, 4),
            "phase": self.phase.name.lower(),
            "elapsed_sec": round(self.elapsed_sec, 1),
            "remaining_sec": round(self.remaining_sec, 1),
            "window_slug": self.window_slug,
        }


CallbackFn = Callable[[WindowState], Awaitable[None]]


class WindowClock:
    """
    Tracks 5-minute window transitions and fires callbacks at phase boundaries.

    Usage:
        clock = WindowClock(entry_window_start=30, entry_deadline=5)
        clock.on_phase_change(my_handler)
        await clock.run(btc_price_getter)
    """

    def __init__(
        self,
        entry_window_start_sec: int = 60,
        entry_deadline_sec: int = 20,
        trading_window_start_sec: int = 45,
        window_duration_sec: int = 300,
    ) -> None:
        self.entry_window_start_sec = entry_window_start_sec
        self.entry_deadline_sec = entry_deadline_sec
        self.trading_window_start_sec = trading_window_start_sec
        self.window_duration_sec = window_duration_sec

        self._phase_callbacks: list[CallbackFn] = []
        self._tick_callbacks: list[CallbackFn] = []
        self._window_open_callbacks: list[CallbackFn] = []
        self._window_close_callbacks: list[CallbackFn] = []

        self._current_window_ts: int = 0
        self._window_open_price: float = 0.0
        self._current_price: float = 0.0
        self._current_phase: WindowPhase = WindowPhase.MONITORING
        self._running: bool = False

    # ── Callback registration ─────────────────────────────────────────────────

    def on_phase_change(self, cb: CallbackFn) -> None:
        """Called whenever the window phase transitions."""
        self._phase_callbacks.append(cb)

    def on_tick(self, cb: CallbackFn) -> None:
        """Called every second with current window state."""
        self._tick_callbacks.append(cb)

    def on_window_open(self, cb: CallbackFn) -> None:
        """Called when a new 5-minute window opens."""
        self._window_open_callbacks.append(cb)

    def on_window_close(self, cb: CallbackFn) -> None:
        """Called when a window closes (at T=0)."""
        self._window_close_callbacks.append(cb)

    # ── Price update ──────────────────────────────────────────────────────────

    def update_price(self, price: float) -> None:
        """Feed the latest BTC price into the clock."""
        self._current_price = price

    def set_window_open_price(self, price: float) -> None:
        """Set the opening price for the current window."""
        self._window_open_price = price

    # ── Current state ─────────────────────────────────────────────────────────

    def get_current_window_ts(self) -> int:
        """Return the Unix timestamp of the current 5-minute window."""
        now = int(time.time())
        return now - (now % self.window_duration_sec)

    def get_state(self) -> WindowState:
        """Return a snapshot of the current window state."""
        now = time.time()
        window_ts = self.get_current_window_ts()
        elapsed = now - window_ts
        remaining = self.window_duration_sec - elapsed

        return WindowState(
            window_ts=window_ts,
            open_price=self._window_open_price,
            current_price=self._current_price,
            phase=self._determine_phase(remaining),
            elapsed_sec=elapsed,
            remaining_sec=remaining,
        )

    def _determine_phase(self, remaining_sec: float) -> WindowPhase:
        """Determine window phase from seconds remaining."""
        if remaining_sec <= 0:
            return WindowPhase.RESOLVED
        elif remaining_sec <= self.entry_deadline_sec:
            return WindowPhase.DEADLINE
        elif remaining_sec <= self.trading_window_start_sec:
            return WindowPhase.TRADING
        elif remaining_sec <= self.entry_window_start_sec:
            return WindowPhase.EVALUATING
        else:
            return WindowPhase.MONITORING

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def run(self) -> None:
        """
        Main clock loop. Runs indefinitely, firing callbacks at phase transitions.
        Must be run as an asyncio task.
        """
        self._running = True
        logger.info("WindowClock started")

        last_window_ts = -1
        last_phase = None

        while self._running:
            now = time.time()
            window_ts = int(now) - (int(now) % self.window_duration_sec)
            elapsed = now - window_ts
            remaining = self.window_duration_sec - elapsed

            # New window opened
            if window_ts != last_window_ts:
                # Fire close callback for the window that just ended.
                # self._window_open_price still holds the closing window's price
                # because _on_window_open for the new window hasn't fired yet.
                if last_window_ts != -1:
                    closed_state = WindowState(
                        window_ts=last_window_ts,
                        open_price=self._window_open_price,
                        current_price=self._current_price,
                        phase=WindowPhase.RESOLVED,
                        elapsed_sec=float(self.window_duration_sec),
                        remaining_sec=0.0,
                    )
                    await self._fire(self._window_close_callbacks, closed_state)

                last_window_ts = window_ts
                self._current_window_ts = window_ts
                # Reset open price (will be set by data layer on first tick)
                # Don't reset _window_open_price here — data layer owns that
                last_phase = None
                logger.info(
                    f"New window opened: ts={window_ts}, "
                    f"slug=btc-updown-5m-{window_ts}"
                )
                state = self.get_state()
                await self._fire(self._window_open_callbacks, state)

            # Phase transition
            current_phase = self._determine_phase(remaining)
            if current_phase != last_phase:
                last_phase = current_phase
                self._current_phase = current_phase
                state = self.get_state()
                logger.info(
                    f"Phase transition → {current_phase.name} "
                    f"({remaining:.1f}s remaining)"
                )
                await self._fire(self._phase_callbacks, state)

            # Tick callbacks (every second)
            state = self.get_state()
            await self._fire(self._tick_callbacks, state)

            # Sleep until next second boundary
            sleep_time = 1.0 - (now % 1.0)
            await asyncio.sleep(sleep_time)

    async def _fire(self, callbacks: list[CallbackFn], state: WindowState) -> None:
        """Fire all callbacks with the given state, catching exceptions."""
        for cb in callbacks:
            try:
                await cb(state)
            except Exception as exc:
                logger.error(f"Callback {cb.__name__} raised: {exc}", exc_info=True)

    def stop(self) -> None:
        """Stop the clock loop."""
        self._running = False
        logger.info("WindowClock stopped")


def get_window_ts(timestamp: float | None = None) -> int:
    """Get the 5-minute window timestamp for a given Unix timestamp (or now)."""
    t = timestamp if timestamp is not None else time.time()
    return int(t) - (int(t) % 300)


def get_next_window_ts(timestamp: float | None = None) -> int:
    """Get the next 5-minute window timestamp."""
    return get_window_ts(timestamp) + 300


def seconds_until_next_window(timestamp: float | None = None) -> float:
    """Seconds until the next window opens."""
    now = timestamp if timestamp is not None else time.time()
    return get_next_window_ts(now) - now


def seconds_into_window(timestamp: float | None = None) -> float:
    """Seconds elapsed since the current window opened."""
    now = timestamp if timestamp is not None else time.time()
    return now - get_window_ts(now)
