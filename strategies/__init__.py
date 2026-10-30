"""Strategy registry."""

from __future__ import annotations

from typing import Any

from strategies.base import BUY, HOLD, SELL, BaseStrategy, StrategyError
from strategies.bollinger import BollingerStrategy
from strategies.breakout import BreakoutStrategy
from strategies.ma_crossover import MACrossoverStrategy
from strategies.momentum import MomentumStrategy
from strategies.rsi_strategy import RSIStrategy

STRATEGIES: dict[str, type[BaseStrategy]] = {
    cls.name: cls
    for cls in (MACrossoverStrategy, RSIStrategy, BollingerStrategy, BreakoutStrategy, MomentumStrategy)
}


def get_strategy(name: str, params: dict[str, Any] | None = None) -> BaseStrategy:
    """Instantiate a registered strategy by name."""
    try:
        cls = STRATEGIES[name]
    except KeyError as exc:
        raise StrategyError(f"Unknown strategy '{name}'. Available: {', '.join(STRATEGIES)}") from exc
    return cls(**(params or {}))


__all__ = ["BUY", "HOLD", "SELL", "STRATEGIES", "BaseStrategy", "StrategyError", "get_strategy"]
