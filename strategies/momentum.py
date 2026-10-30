"""Simple momentum (rate of change) with a long-term trend filter."""

from __future__ import annotations

import pandas as pd

from analysis.indicators import roc, sma
from strategies.base import BaseStrategy, StrategyError, combine, crossed_above, crossed_below


class MomentumStrategy(BaseStrategy):
    """BUY when ROC(period) crosses above ``entry_threshold`` % while price is above its
    ``trend_period`` SMA; SELL when ROC crosses below ``exit_threshold`` %."""

    name = "momentum"
    description = "Rate-of-change momentum with SMA trend filter"
    default_params = {"period": 20, "entry_threshold": 5.0, "exit_threshold": 0.0, "trend_period": 100}
    indicator_columns = ("roc", "trend_sma")

    def validate(self) -> None:
        p = self.params
        if p["period"] < 1 or p["trend_period"] < 0:
            raise StrategyError("period must be >= 1, trend_period >= 0 (0 disables)")
        if p["exit_threshold"] > p["entry_threshold"]:
            raise StrategyError("exit_threshold must be <= entry_threshold")

    @property
    def min_bars(self) -> int:
        return max(self.params["period"], self.params["trend_period"]) + 1

    def compute_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        trend = sma(df["close"], p["trend_period"]) if p["trend_period"] else pd.Series(0.0, index=df.index)
        return df.assign(roc=roc(df["close"], p["period"]), trend_sma=trend)

    def _signals(self, df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
        p = self.params
        buy = crossed_above(df["roc"], p["entry_threshold"]) & (df["close"] > df["trend_sma"])
        sell = crossed_below(df["roc"], p["exit_threshold"])
        value = df["roc"].round(2).astype(str) + "%"
        return combine(
            buy,
            sell,
            f"ROC({p['period']}) > {p['entry_threshold']:g}% in uptrend: " + value,
            f"ROC({p['period']}) < {p['exit_threshold']:g}%: " + value,
        )
