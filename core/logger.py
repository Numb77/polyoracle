"""
Structured JSON logging with console formatting and file rotation.
All modules import the logger from here — never use print().
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Any

from pythonjsonlogger import jsonlogger
from rich.console import Console
from rich.logging import RichHandler
from rich.theme import Theme


# ── Custom log levels ─────────────────────────────────────────────────────────
TRADE_LEVEL = 25  # Between INFO(20) and WARNING(30)
CLAIM_LEVEL = 26  # Same band as TRADE, distinct label for purple UI colouring
logging.addLevelName(TRADE_LEVEL, "TRADE")
logging.addLevelName(CLAIM_LEVEL, "CLAIM")


class TradeLogger(logging.Logger):
    """Extended logger with .trade() and .claim() methods for trade events."""

    def trade(self, msg: str, *args: Any, **kwargs: Any) -> None:
        if self.isEnabledFor(TRADE_LEVEL):
            self._log(TRADE_LEVEL, msg, args, **kwargs)

    def claim(self, msg: str, *args: Any, **kwargs: Any) -> None:
        if self.isEnabledFor(CLAIM_LEVEL):
            self._log(CLAIM_LEVEL, msg, args, **kwargs)


logging.setLoggerClass(TradeLogger)


# ── Color theme for Rich console output ──────────────────────────────────────
_CONSOLE_THEME = Theme(
    {
        "logging.level.debug": "dim white",
        "logging.level.info": "white",
        "logging.level.trade": "bold green",
        "logging.level.warning": "bold yellow",
        "logging.level.error": "bold red",
        "logging.level.critical": "bold red blink",
        "log.time": "dim cyan",
        "log.path": "dim white",
    }
)

_console = Console(theme=_CONSOLE_THEME, highlight=False)


class _TradeRichHandler(RichHandler):
    """RichHandler that colours TRADE level messages in green."""

    pass


def setup_logging(level: str = "INFO", log_file: str = "logs/polyoracle.log") -> None:
    """
    Configure root logger with:
      - Rich console handler (coloured, human-readable)
      - Rotating JSON file handler (structured, machine-readable)

    Call once at startup from core/main.py.
    """
    # Ensure log directory exists
    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    numeric_level = getattr(logging, level.upper(), logging.INFO)

    # ── Root logger ───────────────────────────────────────────────────────────
    root = logging.getLogger()
    root.setLevel(numeric_level)
    root.handlers.clear()

    # ── Console handler (Rich, coloured) ─────────────────────────────────────
    console_handler = _TradeRichHandler(
        console=_console,
        show_time=True,
        show_path=True,
        rich_tracebacks=True,
        tracebacks_show_locals=False,
        markup=False,
    )
    console_handler.setLevel(numeric_level)
    root.addHandler(console_handler)

    # ── Rotating JSON file handler ────────────────────────────────────────────
    file_handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=50 * 1024 * 1024,   # 50 MB per file
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(numeric_level)

    json_formatter = jsonlogger.JsonFormatter(
        fmt="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        rename_fields={"levelname": "level", "name": "module"},
    )
    file_handler.setFormatter(json_formatter)
    root.addHandler(file_handler)

    # Silence noisy third-party loggers
    for noisy in ["websockets", "aiohttp", "urllib3", "web3", "asyncio"]:
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> TradeLogger:
    """Get a named logger. Always use this instead of logging.getLogger()."""
    return logging.getLogger(name)  # type: ignore[return-value]


class _DashboardLogHandler(logging.Handler):
    """
    Logging handler that forwards CLAIM-level log records to the dashboard
    WebSocket terminal so that claim events appear in purple.

    Only CLAIM-level records are forwarded — other levels are already pushed
    explicitly via dashboard.push_log() from main.py, so we don't duplicate.
    """

    def __init__(self, dashboard: Any) -> None:
        super().__init__()
        self._dashboard = dashboard

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.levelname != "CLAIM":
                return
            module = record.name.split(".")[-1]
            message = self.format(record)
            self._dashboard.push_log("CLAIM", module, message)
        except Exception:
            pass  # Never raise from a logging handler


def add_dashboard_handler(dashboard: Any) -> None:
    """
    Install a log handler that forwards CLAIM-level Python log records to the
    dashboard WebSocket terminal (shown in purple).

    Call once from main.py after the DashboardServer is created.
    """
    handler = _DashboardLogHandler(dashboard)
    handler.setLevel(CLAIM_LEVEL)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logging.getLogger().addHandler(handler)
