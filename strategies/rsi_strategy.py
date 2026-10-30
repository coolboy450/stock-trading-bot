"""RSI oversold / overbought (mean reversion)."""

from __future__ import annotations

import pandas as pd

from analysis.indicators import rsi
from strategies.base import BaseStrategy, StrategyError, combine, crossed_above


class RSIStrategy(BaseStrategy):
    """BUY when RSI crosses back above ``oversold``; SELL when it crosses above ``overbought``."""

    name = "rsi"
    description = "RSI oversold/overbought reversal"
    default_params = {"period": 14, "oversold": 30.0, "overbought": 70.0}
    indicator_columns = ("rsi",)

    def validate(self) -> None:
        p = self.params
        if p["period"] < 2:
            raise StrategyError("period must be >= 2")
        if not 0 < p["oversold"] < p["overbought"] < 100:
            raise StrategyError("require 0 < oversold < overbought < 100")

    @property
    def min_bars(self) -> int:
        return self.params["period"] * 3

    def compute_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        return df.assign(rsi=rsi(df["close"], self.params["period"]))

    def _signals(self, df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
        p = self.params
        buy = crossed_above(df["rsi"], p["oversold"])
        sell = crossed_above(df["rsi"], p["overbought"])
        value = df["rsi"].round(1).astype(str)
        return combine(
            buy,
            sell,
            f"RSI({p['period']}) recovered above oversold {p['oversold']:g}: " + value,
            f"RSI({p['period']}) above overbought {p['overbought']:g}: " + value,
        )
