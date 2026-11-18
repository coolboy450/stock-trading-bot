"""Moving-average crossover (trend following)."""

from __future__ import annotations

import pandas as pd

from analysis.indicators import ema, sma
from strategies.base import BaseStrategy, StrategyError, combine, crossed_above, crossed_below


class MACrossoverStrategy(BaseStrategy):
    """SELL when the fast MA crosses below the slow MA (both entry modes).

    ``entry_mode``:
      * ``cross`` (default): BUY only on the golden-cross bar.
      * ``trend``: BUY on any bar where fast > slow, close > fast and fast is higher than
        ``trend_slope_bars`` bars ago. After a stop-loss the engines wait ``cooldown_bars``
        bars before re-entering that symbol (see ``entry_cooldown_bars``).
    """

    name = "ma_crossover"
    description = "Fast/slow SMA or EMA crossover"
    default_params = {
        "fast_period": 20, "slow_period": 50, "ma_type": "ema",
        "entry_mode": "cross", "trend_slope_bars": 5, "cooldown_bars": 5,
    }
    indicator_columns = ("ma_fast", "ma_slow")

    def validate(self) -> None:
        p = self.params
        if p["fast_period"] < 1 or p["slow_period"] < 2:
            raise StrategyError("periods must be positive")
        if p["fast_period"] >= p["slow_period"]:
            raise StrategyError("fast_period must be < slow_period")
        if p["ma_type"] not in {"sma", "ema"}:
            raise StrategyError("ma_type must be 'sma' or 'ema'")
        if p["entry_mode"] not in {"cross", "trend"}:
            raise StrategyError("entry_mode must be 'cross' or 'trend'")
        if p["trend_slope_bars"] < 1 or p["cooldown_bars"] < 0:
            raise StrategyError("trend_slope_bars must be >= 1 and cooldown_bars >= 0")

    @property
    def entry_cooldown_bars(self) -> int:
        """Bars to wait after a stop-loss before re-entering (trend mode only; cross mode = 0)."""
        return self.params["cooldown_bars"] if self.params["entry_mode"] == "trend" else 0

    @property
    def min_bars(self) -> int:
        extra = self.params["trend_slope_bars"] if self.params["entry_mode"] == "trend" else 0
        return self.params["slow_period"] + 1 + extra

    def compute_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        ma = ema if self.params["ma_type"] == "ema" else sma
        return df.assign(
            ma_fast=ma(df["close"], self.params["fast_period"]),
            ma_slow=ma(df["close"], self.params["slow_period"]),
        )

    def _signals(self, df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
        p, kind = self.params, self.params["ma_type"].upper()
        label = f"{kind}{p['fast_period']}/{kind}{p['slow_period']}"
        if p["entry_mode"] == "trend":
            slope = p["trend_slope_bars"]
            buy = (
                (df["ma_fast"] > df["ma_slow"])
                & (df["close"] > df["ma_fast"])
                & (df["ma_fast"] > df["ma_fast"].shift(slope))
            )
            buy_label = f"Trend entry {label} (close > fast, fast rising {slope} bars)"
        else:
            buy = crossed_above(df["ma_fast"], df["ma_slow"])
            buy_label = f"Golden cross {label}"
        sell = crossed_below(df["ma_fast"], df["ma_slow"])
        txt = df["ma_fast"].round(2).astype(str) + " vs " + df["ma_slow"].round(2).astype(str)
        return combine(buy, sell, buy_label + ": " + txt, f"Death cross {label}: " + txt)
