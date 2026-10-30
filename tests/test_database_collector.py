from datetime import datetime

import pandas as pd
import pytest
from sqlalchemy import select

from analysis.daily_summary import build_daily_summary, format_report
from data.collector import PriceCollector
from data.providers import DataProviderError, FallbackProvider, SyntheticProvider
from data.providers.base import DataProvider, normalize_ohlcv
from database import repository as repo
from database.models import DailySummary, Direction, PriceHistory
from tests.conftest import FIXED_END, make_ohlcv


class FailingProvider(DataProvider):
    name = "failing"

    def fetch_history(self, symbol, interval="1d", period=None, start=None, end=None):
        raise DataProviderError("network down")


def test_upsert_computes_changes_and_is_idempotent(db):
    df = make_ohlcv([100, 105, 102.9, 102.9])
    with db.session() as s:
        stock = repo.get_or_create_stock(s, "aaa", market="US")
        assert repo.upsert_prices(s, stock, df, "1d", "test") == 4
        assert repo.upsert_prices(s, stock, df, "1d", "test") == 0  # nothing changed
        bars = list(s.scalars(select(PriceHistory).order_by(PriceHistory.timestamp)))
        assert [b.direction for b in bars] == [None, Direction.UP, Direction.DOWN, Direction.UNCHANGED]
        assert bars[1].percent_change == pytest.approx(5.0)
        assert bars[2].percent_change == pytest.approx(-2.0)
        assert bars[1].prev_close == 100


def test_backfill_older_history_fixes_changes(db):
    full = make_ohlcv([100, 110, 121])
    with db.session() as s:
        stock = repo.get_or_create_stock(s, "AAA")
        repo.upsert_prices(s, stock, full.iloc[1:], "1d", "t")  # newest first
        first = repo.get_price_df(s, stock.id, "1d")
        assert pd.isna(first["percent_change"].iloc[0])
        repo.upsert_prices(s, stock, full.iloc[:1], "1d", "t")  # back-fill older bar
        df = repo.get_price_df(s, stock.id, "1d")
        assert df["percent_change"].round(6).tolist()[1:] == [10.0, 10.0]


def test_synthetic_provider_is_deterministic_and_valid():
    p = SyntheticProvider(end=FIXED_END)
    a = p.fetch_history("AAA", period="1y")
    b = p.fetch_history("AAA", start=datetime(2026, 1, 1))
    assert len(a) > 200
    assert (a["high"] >= a["low"]).all() and (a["close"] > 0).all()
    overlap = a.index.intersection(b.index)
    assert len(overlap) > 50
    pd.testing.assert_frame_equal(a.loc[overlap], b.loc[overlap])


def test_normalize_handles_tz_and_bad_rows():
    idx = pd.date_range("2026-01-01", periods=3, freq="D", tz="Asia/Bangkok")
    raw = pd.DataFrame({"Open": [1, 2, None], "High": [1.1, 2.2, 3], "Low": [0.9, 1.8, 2],
                        "Close": [1, 2, 3], "Volume": [10, 20, 30]}, index=idx)
    out = normalize_ohlcv(raw)
    assert out.index.tz is None and len(out) == 2
    assert out.index[0] == pd.Timestamp("2025-12-31 17:00")  # converted to UTC


def test_fallback_provider():
    fb = FallbackProvider([FailingProvider(), SyntheticProvider(end=FIXED_END)])
    df = fb.fetch_history("AAA", period="1mo")
    assert not df.empty and fb.last_source == "synthetic"
    with pytest.raises(DataProviderError):
        FallbackProvider([FailingProvider()]).fetch_history("AAA")


def test_collector_backfill_update_and_failure(db, settings, watch_item):
    collector = PriceCollector(settings, SyntheticProvider(end=FIXED_END))
    with db.session() as s:
        res = collector.backfill(s, watch_item, period="1y")
        assert res.ok and res.bars_written > 200
        again = collector.update(s, watch_item)
        assert again.ok and again.bars_written == 0
    bad = PriceCollector(settings, FailingProvider())
    with db.session() as s:
        res = bad.update(s, watch_item)
        assert not res.ok and "network down" in res.error


def test_daily_summary(db, settings):
    collector = PriceCollector(settings, SyntheticProvider(end=FIXED_END))
    with db.session() as s:
        for item in settings.watchlist:
            collector.backfill(s, item, period="1mo")
        rows = build_daily_summary(s, settings)
        assert len(rows) == 2
        assert all(r.direction in {Direction.UP, Direction.DOWN, Direction.UNCHANGED} for r in rows)
        stored = list(s.scalars(select(DailySummary)))
        assert len(stored) == 2
        # matches the stored bar's percent change
        stock = repo.get_stock(s, "AAA")
        last = repo.last_bar(s, stock.id, "1d")
        row = next(r for r in stored if r.stock_id == stock.id)
        assert row.percent_change == pytest.approx(last.percent_change)
    report = format_report(rows)
    assert "Daily summary" in report and "AAA" in report
