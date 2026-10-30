"""Price change (up/down) calculations."""

from __future__ import annotations

import pandas as pd

from database.models import Direction

#: Moves smaller than this (in %) are recorded as UNCHANGED.
UNCHANGED_TOLERANCE_PCT = 1e-9


def classify_direction(percent_change: float | None) -> Direction | None:
    """Map a % change to UP / DOWN / UNCHANGED."""
    if percent_change is None or pd.isna(percent_change):
        return None
    if percent_change > UNCHANGED_TOLERANCE_PCT:
        return Direction.UP
    if percent_change < -UNCHANGED_TOLERANCE_PCT:
        return Direction.DOWN
    return Direction.UNCHANGED


def compute_changes(df: pd.DataFrame, prev_close: float | None = None) -> pd.DataFrame:
    """Add ``prev_close``, ``change``, ``percent_change`` and ``direction`` columns.

    Args:
        df: OHLCV frame sorted (or sortable) by its datetime index; needs ``close``.
        prev_close: Close of the bar immediately before ``df`` (e.g. last stored bar),
            so the first row also gets a change value.
    """
    out = df.sort_index().copy()
    prev = out["close"].shift(1)
    if len(out) and prev_close is not None:
        prev.iloc[0] = prev_close
    out["prev_close"] = prev
    out["change"] = out["close"] - prev
    out["percent_change"] = (out["change"] / prev.where(prev != 0)) * 100.0
    out["direction"] = pd.Series(
        [classify_direction(v) for v in out["percent_change"]], index=out.index, dtype=object
    )
    return out
