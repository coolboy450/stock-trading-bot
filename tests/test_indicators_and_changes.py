import numpy as np
import pandas as pd
import pytest

from analysis.indicators import atr, bollinger_bands, donchian, ema, roc, rsi, sma
from analysis.price_change import classify_direction, compute_changes
from database.models import Direction
from tests.conftest import make_ohlcv


def test_sma_and_ema_basic():
    s = pd.Series([1, 2, 3, 4, 5], dtype=float)
    assert sma(s, 3).tolist()[2:] == [2.0, 3.0, 4.0]
    assert sma(s, 3).isna().sum() == 2
    e = ema(s, 3)
    assert e.isna().sum() == 2 and e.iloc[-1] > e.iloc[-2]


def test_rsi_bounds_and_extremes():
    up = pd.Series(np.arange(1, 50, dtype=float))
    assert rsi(up, 14).dropna().iloc[-1] == pytest.approx(100.0)
    down = pd.Series(np.arange(50, 1, -1, dtype=float))
    assert rsi(down, 14).dropna().iloc[-1] == pytest.approx(0.0)
    rand = pd.Series(np.random.default_rng(1).normal(0, 1, 300).cumsum() + 100)
    r = rsi(rand, 14).dropna()
    assert ((r >= 0) & (r <= 100)).all()


def test_bollinger_band_order():
    s = pd.Series(np.random.default_rng(2).normal(0, 1, 100).cumsum() + 50)
    bb = bollinger_bands(s, 20, 2).dropna()
    assert (bb["bb_upper"] >= bb["bb_mid"]).all() and (bb["bb_mid"] >= bb["bb_lower"]).all()


def test_donchian_excludes_current_bar():
    df = make_ohlcv([10, 11, 12, 13, 20])
    dc = donchian(df, 3)
    # the 20 close on the last bar must not be in its own channel
    assert dc["dc_upper"].iloc[-1] < df["high"].iloc[-1]


def test_atr_and_roc():
    df = make_ohlcv(np.linspace(100, 110, 30))
    assert atr(df, 14).dropna().gt(0).all()
    assert roc(df["close"], 1).iloc[1] == pytest.approx((df["close"].iloc[1] / df["close"].iloc[0] - 1) * 100)


def test_compute_changes_up_down_unchanged():
    df = make_ohlcv([100, 110, 99, 99])
    out = compute_changes(df, prev_close=95)
    assert out["percent_change"].round(4).tolist() == [round(5 / 95 * 100, 4), 10.0, -10.0, 0.0]
    assert out["direction"].tolist() == [Direction.UP, Direction.UP, Direction.DOWN, Direction.UNCHANGED]
    assert out["change"].iloc[2] == pytest.approx(-11)


def test_compute_changes_first_row_without_prev():
    out = compute_changes(make_ohlcv([100, 101]))
    assert pd.isna(out["percent_change"].iloc[0]) and out["direction"].iloc[0] is None


def test_classify_direction():
    assert classify_direction(None) is None
    assert classify_direction(0.5) == Direction.UP
    assert classify_direction(-0.5) == Direction.DOWN
    assert classify_direction(0.0) == Direction.UNCHANGED
