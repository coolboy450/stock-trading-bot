"""Market-data provider interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

import pandas as pd
from loguru import logger

REQUIRED_COLUMNS = ["open", "high", "low", "close", "volume"]


class DataProviderError(RuntimeError):
    """Raised when a provider cannot return data (network, rate limit, bad symbol...)."""


class DataProvider(ABC):
    """Returns OHLCV bars as a DataFrame with a naive-UTC ``DatetimeIndex`` and
    columns ``open, high, low, close, volume`` (+ optional ``adj_close``)."""

    name: str = "base"

    @abstractmethod
    def fetch_history(
        self,
        symbol: str,
        interval: str = "1d",
        period: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> pd.DataFrame:
        """Fetch bars for ``symbol``. Use either ``period`` (e.g. ``"2y"``) or ``start``/``end``."""

    def fetch_latest_price(self, symbol: str) -> float | None:
        """Most recent close (default: last 1d bar of the past few days)."""
        df = self.fetch_history(symbol, interval="1d", period="5d")
        return None if df.empty else float(df["close"].iloc[-1])


def normalize_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    """Lower-case columns, coerce types, convert index to naive UTC, drop bad rows."""
    if df is None or df.empty:
        return pd.DataFrame(columns=REQUIRED_COLUMNS)
    out = df.copy()
    out.columns = [str(c).strip().lower().replace(" ", "_") for c in out.columns]
    missing = [c for c in REQUIRED_COLUMNS if c not in out.columns]
    if missing:
        raise DataProviderError(f"Provider data missing columns: {missing}")
    keep = REQUIRED_COLUMNS + (["adj_close"] if "adj_close" in out.columns else [])
    out = out[keep].apply(pd.to_numeric, errors="coerce")

    idx = pd.DatetimeIndex(pd.to_datetime(out.index))
    if idx.tz is not None:
        idx = idx.tz_convert("UTC").tz_localize(None)
    out.index = idx.as_unit("us")
    out.index.name = "timestamp"

    before = len(out)
    out = out.dropna(subset=["open", "high", "low", "close"])
    out = out[(out["close"] > 0) & (out["high"] >= out["low"])]
    out["volume"] = out["volume"].fillna(0)
    if len(out) < before:
        logger.debug("Dropped {} invalid bars", before - len(out))
    out = out[~out.index.duplicated(keep="last")].sort_index()
    return out
