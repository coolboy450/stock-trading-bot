"""Deterministic synthetic prices (geometric Brownian motion with regime shifts).

For offline demos, tests and CI - **not** real market data.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from data.providers.base import DataProvider, normalize_ohlcv

_STEP = {"1m": "1min", "5m": "5min", "15m": "15min", "30m": "30min", "1h": "1h", "60m": "1h", "1d": "B"}
_PERIOD_DAYS = {"d": 1, "wk": 7, "mo": 30, "y": 365}


def _period_to_days(period: str) -> int:
    for suffix in sorted(_PERIOD_DAYS, key=len, reverse=True):
        if period.endswith(suffix) and period[: -len(suffix)].isdigit():
            return int(period[: -len(suffix)]) * _PERIOD_DAYS[suffix]
    if period == "max":
        return 365 * 10
    raise ValueError(f"Unsupported period: {period}")


class SyntheticProvider(DataProvider):
    name = "synthetic"

    def __init__(self, seed: int = 42, start_price: float = 100.0, end: datetime | None = None) -> None:
        self.seed = seed
        self.start_price = start_price
        self.fixed_end = end  # fix the end date for reproducible tests

    def _streams(self, symbol: str, interval: str, count: int) -> list[np.random.Generator]:
        """Independent random streams, so each series is prefix-stable as ``n`` grows."""
        digest = hashlib.sha256(f"{self.seed}:{symbol}:{interval}".encode()).hexdigest()
        seq = np.random.SeedSequence(int(digest[:12], 16))
        return [np.random.default_rng(child) for child in seq.spawn(count)]

    def fetch_history(
        self,
        symbol: str,
        interval: str = "1d",
        period: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> pd.DataFrame:
        end_dt = end or self.fixed_end or datetime.now(timezone.utc).replace(tzinfo=None, second=0, microsecond=0)
        start_dt = start or end_dt - timedelta(days=_period_to_days(period or "1y"))
        # Generate from a fixed anchor so overlapping requests return identical bars
        # (intraday: anchored to the 1st of the month, 2 months before start).
        step = _STEP.get(interval, "B")
        if interval == "1d":
            # Daily bars stamped 12:00 UTC so the local trading date is right for both US and TH.
            anchor = datetime(2010, 1, 1, 12)
        else:
            month_start = start_dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            anchor = (month_start - timedelta(days=45)).replace(day=1)
        index = pd.date_range(anchor, end_dt, freq=step)
        n = len(index)
        r_param, r_regime, r_ret, r_open, r_high, r_low, r_vol = self._streams(symbol, interval, 7)

        per_year = 252 if interval == "1d" else 252 * 390 / max(1, int(pd.Timedelta(step).total_seconds() // 60))
        # Regime-switching drift makes trends + ranges, so strategies have something to do.
        regime_len = 60 if interval == "1d" else 390
        drifts = r_regime.choice([-0.25, 0.05, 0.35], size=n // regime_len + 1, p=[0.3, 0.35, 0.35])
        mu = np.repeat(drifts, regime_len)[:n] / per_year
        vol_annual, level = r_param.uniform(0.18, 0.40), r_param.uniform(0.5, 2.5)
        sigma = vol_annual / np.sqrt(per_year)
        rets = mu + sigma * r_ret.standard_normal(n)
        close = self.start_price * level * np.exp(np.cumsum(rets))

        open_ = np.concatenate([[close[0]], close[:-1]]) * (1 + r_open.normal(0, sigma / 3, n))
        high = np.maximum(open_, close) * (1 + np.abs(r_high.normal(0, sigma / 2, n)))
        low = np.minimum(open_, close) * (1 - np.abs(r_low.normal(0, sigma / 2, n)))
        volume = r_vol.lognormal(13, 0.4, n).round()

        df = pd.DataFrame(
            {"open": open_, "high": high, "low": low, "close": close, "volume": volume, "adj_close": close},
            index=index,
        )
        df = df[(df.index >= pd.Timestamp(start_dt)) & (df.index <= pd.Timestamp(end_dt))]
        return normalize_ohlcv(df.round(4))
