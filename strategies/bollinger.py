"""Bollinger Bands mean reversion."""

from __future__ import annotations

import pandas as pd

from analysis.indicators import bollinger_bands
from strategies.base import BaseStrategy, StrategyError, combine, crossed_above


class BollingerStrategy(BaseStrategy):
    """BUY when close re-enters above the lower band; SELL at the middle or upper band."""

    name = "bollinger"
    description = "Bollinger Bands lower-band reversal"
    default_params = {"period": 20, "std_dev": 2.0, "exit_at": "middle"}
    indicator_columns = ("bb_lower", "bb_mid", "bb_upper")

    def validate(self) -> None:
        if self.params["period"] < 2 or self.params["std_dev"] <= 0:
            raise StrategyError("period must be >= 2 and std_dev > 0")
        if self.params["exit_at"] not in {"middle", "upper"}:
            raise StrategyError("exit_at must be 'middle' or 'upper'")

    @property
    def min_bars(self) -> int:
        return self.params["period"] + 1

    def compute_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        bands = bollinger_bands(df["close"], self.params["period"], self.params["std_dev"])
        return pd.concat([df, bands], axis=1)

    def _signals(self, df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
        exit_col = "bb_mid" if self.params["exit_at"] == "middle" else "bb_upper"
        buy = crossed_above(df["close"], df["bb_lower"])
        sell = crossed_above(df["close"], df[exit_col])
        close = df["close"].round(2).astype(str)
        return combine(
            buy,
            sell,
            "Close back above lower band " + df["bb_lower"].round(2).astype(str) + ": " + close,
            f"Close crossed {self.params['exit_at']} band " + df[exit_col].round(2).astype(str) + ": " + close,
        )
