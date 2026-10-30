"""Technical indicators (pure pandas, no look-ahead)."""

from __future__ import annotations

import numpy as np
import pandas as pd


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period, min_periods=period).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI (0-100)."""
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    # No losses in the window -> RSI 100; no movement at all -> 50.
    out = out.where(avg_loss != 0, np.where(avg_gain > 0, 100.0, 50.0))
    return out.where(avg_gain.notna())


def bollinger_bands(series: pd.Series, period: int = 20, std_dev: float = 2.0) -> pd.DataFrame:
    mid = sma(series, period)
    std = series.rolling(period, min_periods=period).std(ddof=0)
    return pd.DataFrame({"bb_mid": mid, "bb_upper": mid + std_dev * std, "bb_lower": mid - std_dev * std})


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def donchian(df: pd.DataFrame, period: int) -> pd.DataFrame:
    """Highest high / lowest low of the *previous* ``period`` bars (excludes current bar)."""
    return pd.DataFrame(
        {
            "dc_upper": df["high"].shift(1).rolling(period, min_periods=period).max(),
            "dc_lower": df["low"].shift(1).rolling(period, min_periods=period).min(),
        }
    )


def roc(series: pd.Series, period: int) -> pd.Series:
    """Rate of change in %."""
    return (series / series.shift(period) - 1) * 100
