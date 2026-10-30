from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import Settings, WatchItem  # noqa: E402
from database.session import Database  # noqa: E402


@pytest.fixture
def db() -> Database:
    database = Database("sqlite:///:memory:")
    database.create_all()
    return database


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings.model_validate(
        {
            "database": {"url": "sqlite:///:memory:"},
            "data": {"provider": "synthetic", "interval": "1d", "lookback_bars": 300},
            "watchlist": [
                {"symbol": "AAA", "market": "US"},
                {"symbol": "BBB", "market": "US"},
            ],
            "strategy": {"name": "ma_crossover", "params": {"ma_crossover": {"fast_period": 5, "slow_period": 20}}},
            "alerts": {"channels": ["console"], "cooldown_minutes": 0},
            "logging": {"directory": str(tmp_path / "logs")},
            "execution": {"portfolio_name": "test", "initial_capital": 100_000},
        }
    )


def make_ohlcv(closes: list[float] | np.ndarray, start: str = "2024-01-01", freq: str = "B") -> pd.DataFrame:
    closes = np.asarray(closes, dtype=float)
    idx = pd.date_range(start, periods=len(closes), freq=freq)
    opens = np.concatenate([[closes[0]], closes[:-1]])
    return pd.DataFrame(
        {
            "open": opens,
            "high": np.maximum(opens, closes) * 1.005,
            "low": np.minimum(opens, closes) * 0.995,
            "close": closes,
            "volume": np.full(len(closes), 1_000_000.0),
        },
        index=idx,
    )


@pytest.fixture
def trending_df() -> pd.DataFrame:
    """Down, then strong up-trend, then down: guarantees MA crossovers."""
    down = np.linspace(120, 80, 60)
    up = np.linspace(80, 160, 80)
    down2 = np.linspace(160, 110, 60)
    return make_ohlcv(np.concatenate([down, up, down2]))


@pytest.fixture
def watch_item() -> WatchItem:
    return WatchItem(symbol="AAA", market="US")


FIXED_END = datetime(2026, 6, 30, 23, 0)
