"""Central logging setup (loguru) - console + rotating files + error file."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from loguru import logger

from config.settings import PROJECT_ROOT, LoggingSettings


class _InterceptHandler(logging.Handler):
    """Route stdlib ``logging`` records (SQLAlchemy, APScheduler, uvicorn...) to loguru."""

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - glue code
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        frame, depth = logging.currentframe(), 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def setup_logging(settings: LoggingSettings) -> None:
    """Configure loguru sinks. Safe to call more than once."""
    log_dir = Path(settings.directory)
    if not log_dir.is_absolute():
        log_dir = PROJECT_ROOT / log_dir
    log_dir.mkdir(parents=True, exist_ok=True)

    logger.remove()
    logger.add(
        sys.stderr,
        level=settings.level,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
    )
    logger.add(
        log_dir / "stock_bot.log",
        level=settings.level,
        rotation=settings.rotation,
        retention=settings.retention,
        encoding="utf-8",
        enqueue=True,
        serialize=settings.json_logs,
    )
    logger.add(
        log_dir / "errors.log",
        level="ERROR",
        rotation=settings.rotation,
        retention=settings.retention,
        encoding="utf-8",
        enqueue=True,
        backtrace=True,
        diagnose=False,  # don't dump local variables (may contain secrets)
    )
    logger.add(
        log_dir / "trades.log",
        level="INFO",
        filter=lambda record: record["extra"].get("category") == "trade",
        rotation=settings.rotation,
        retention=settings.retention,
        encoding="utf-8",
        enqueue=True,
    )
    logging.basicConfig(handlers=[_InterceptHandler()], level=logging.WARNING, force=True)


#: Use ``trade_logger.info(...)`` for order/trade events -> also written to trades.log
trade_logger = logger.bind(category="trade")
