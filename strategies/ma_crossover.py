"""Moving-average crossover (trend following)."""

from __future__ import annotations

import pandas as pd

from analysis.indicators import ema, sma
from strategies.base import BaseStrategy, StrategyError, combine, crossed_above, crossed_below


class MACrossoverStrategy(BaseStrategy):
    """BUY when the fast MA crosses above the slow MA; SELL when it crosses below."""

    name = "ma_crossover"
    description = "Fast/slow SMA or EMA crossover"
    default_params = {"fast_period": 20, "slow_period": 50, "ma_type": "ema"}
    indicator_columns = ("ma_fast", "ma_slow")

    def validate(self) -> None:
        p = self.params
        if p["fast_period"] < 1 or p["slow_period"] < 2:
            raise StrategyError("periods must be positive")
        if p["fast_period"] >= p["slow_period"]:
            raise StrategyError("fast_period must be < slow_period")
        if p["ma_type"] not in {"sma", "ema"}:
            raise StrategyError("ma_type must be 'sma' or 'ema'")

    @property
    def min_bars(self) -> int:
        return self.params["slow_period"] + 1

    def compute_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        ma = ema if self.params["ma_type"] == "ema" else sma
        return df.assign(
            ma_fast=ma(df["close"], self.params["fast_period"]),
            ma_slow=ma(df["close"], self.params["slow_period"]),
        )

    def _signals(self, df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
        p, kind = self.params, self.params["ma_type"].upper()
        label = f"{kind}{p['fast_period']}/{kind}{p['slow_period']}"
        buy = crossed_above(df["ma_fast"], df["ma_slow"])
        sell = crossed_below(df["ma_fast"], df["ma_slow"])
        txt = df["ma_fast"].round(2).astype(str) + " vs " + df["ma_slow"].round(2).astype(str)
        return combine(buy, sell, f"Golden cross {label}: " + txt, f"Death cross {label}: " + txt)
