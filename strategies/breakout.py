"""Donchian channel breakout (trend following)."""

from __future__ import annotations

import pandas as pd

from analysis.indicators import donchian, sma
from strategies.base import BaseStrategy, StrategyError, combine


class BreakoutStrategy(BaseStrategy):
    """BUY on a close above the prior ``entry_period``-bar high (optionally with a volume
    surge); SELL on a close below the prior ``exit_period``-bar low."""

    name = "breakout"
    description = "Donchian channel breakout with volume filter"
    default_params = {"entry_period": 20, "exit_period": 10, "volume_factor": 1.0}
    indicator_columns = ("dc_upper", "exit_low", "vol_avg")

    def validate(self) -> None:
        p = self.params
        if p["entry_period"] < 2 or p["exit_period"] < 2:
            raise StrategyError("periods must be >= 2")
        if p["volume_factor"] < 0:
            raise StrategyError("volume_factor must be >= 0")

    @property
    def min_bars(self) -> int:
        return max(self.params["entry_period"], self.params["exit_period"]) + 1

    def compute_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        entry = donchian(df, p["entry_period"])
        exit_ = donchian(df, p["exit_period"])
        return df.assign(
            dc_upper=entry["dc_upper"],
            exit_low=exit_["dc_lower"],
            vol_avg=sma(df["volume"], p["entry_period"]).shift(1),
        )

    def _signals(self, df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
        p = self.params
        breakout = df["close"] > df["dc_upper"]
        first_breakout = breakout & ~breakout.shift(1, fill_value=False)
        volume_ok = df["volume"] >= df["vol_avg"] * p["volume_factor"] if p["volume_factor"] > 0 else True
        buy = first_breakout & volume_ok
        breakdown = df["close"] < df["exit_low"]
        sell = breakdown & ~breakdown.shift(1, fill_value=False)
        close = df["close"].round(2).astype(str)
        return combine(
            buy,
            sell,
            f"Breakout above {p['entry_period']}-bar high " + df["dc_upper"].round(2).astype(str) + ": " + close,
            f"Breakdown below {p['exit_period']}-bar low " + df["exit_low"].round(2).astype(str) + ": " + close,
        )
