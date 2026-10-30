"""Strategy interface.

A strategy turns an OHLCV DataFrame into per-bar signals:

* ``signal``: ``1`` = BUY, ``-1`` = SELL, ``0`` = HOLD
* ``reason``: human-readable explanation (stored in the ``signals`` table)

Signals on bar *t* must only use data up to and including bar *t*; the
backtester executes them on the **next** bar's open to avoid look-ahead bias.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar

import pandas as pd

BUY, SELL, HOLD = 1, -1, 0


class StrategyError(ValueError):
    """Invalid strategy parameters."""


class BaseStrategy(ABC):
    name: ClassVar[str] = "base"
    description: ClassVar[str] = ""
    default_params: ClassVar[dict[str, Any]] = {}

    def __init__(self, **params: Any) -> None:
        unknown = set(params) - set(self.default_params)
        if unknown:
            raise StrategyError(f"{self.name}: unknown parameter(s) {sorted(unknown)}")
        merged = {**self.default_params, **params}
        # Coerce to the default's type (YAML / CLI values may arrive as strings).
        for key, default in self.default_params.items():
            if default is not None and not isinstance(merged[key], type(default)):
                try:
                    merged[key] = type(default)(merged[key])
                except (TypeError, ValueError) as exc:
                    raise StrategyError(f"{self.name}: bad value for {key}: {merged[key]!r}") from exc
        self.params: dict[str, Any] = merged
        self.validate()

    def validate(self) -> None:
        """Override to check parameter consistency; raise ``StrategyError``."""

    @property
    @abstractmethod
    def min_bars(self) -> int:
        """Bars needed before the first valid signal."""

    @abstractmethod
    def compute_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Return a copy of ``df`` with indicator columns added."""

    @abstractmethod
    def _signals(self, df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
        """Return (signal, reason) series for a frame that already has indicators."""

    #: Indicator columns copied into ``signals.indicators`` (JSON) in the DB.
    indicator_columns: ClassVar[tuple[str, ...]] = ()

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            return df.assign(signal=pd.Series(dtype=int), reason=pd.Series(dtype=object))
        out = self.compute_indicators(df)
        signal, reason = self._signals(out)
        out["signal"] = signal.fillna(HOLD).astype(int)
        out["reason"] = reason.where(out["signal"] != HOLD, "")
        return out

    def latest_indicators(self, df_with_signals: pd.DataFrame) -> dict[str, float | None]:
        last = df_with_signals.iloc[-1]
        return {c: (None if pd.isna(last[c]) else round(float(last[c]), 6)) for c in self.indicator_columns if c in last}

    def __repr__(self) -> str:
        args = ", ".join(f"{k}={v}" for k, v in self.params.items())
        return f"{type(self).__name__}({args})"


def crossed_above(a: pd.Series, b: pd.Series | float) -> pd.Series:
    b_prev = b.shift(1) if isinstance(b, pd.Series) else b
    return (a > b) & (a.shift(1) <= b_prev)


def crossed_below(a: pd.Series, b: pd.Series | float) -> pd.Series:
    b_prev = b.shift(1) if isinstance(b, pd.Series) else b
    return (a < b) & (a.shift(1) >= b_prev)


def combine(buy: pd.Series, sell: pd.Series, buy_reason: pd.Series, sell_reason: pd.Series) -> tuple[pd.Series, pd.Series]:
    """Build (signal, reason) from boolean buy/sell masks (SELL wins ties)."""
    signal = pd.Series(HOLD, index=buy.index)
    signal[buy.fillna(False)] = BUY
    signal[sell.fillna(False)] = SELL
    reason = pd.Series("", index=buy.index, dtype=object)
    reason[signal == BUY] = buy_reason[signal == BUY]
    reason[signal == SELL] = sell_reason[signal == SELL]
    return signal, reason
