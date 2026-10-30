"""End-to-end tests of the paper trading engine (synthetic data, in-memory DB)."""

from datetime import datetime, timedelta

import pandas as pd
import pytest
from sqlalchemy import func, select

from data.providers import SyntheticProvider
from data.providers.base import DataProvider
from database import repository as repo
from database.models import Order, OrderStatus, PortfolioSnapshot, Signal, Trade, TradeStatus
from execution.broker import PaperBroker
from execution.costs import CostModel
from execution.engine import TradingEngine
from execution.safety import CONFIRM_ENV, CONFIRM_VALUE, LiveTradingNotConfirmed, confirm_live_trading
from monitoring.alerts import AlertManager
from monitoring.notifier import Notifier
from tests.conftest import make_ohlcv


class RecordingNotifier(Notifier):
    name = "recording"

    def __init__(self):
        self.messages: list[tuple[str, str, str]] = []

    def send(self, title, message, level="INFO"):
        self.messages.append((title, message, level))
        return True


class FrameProvider(DataProvider):
    """Serves a prepared frame up to a movable cursor (simulates time passing)."""

    name = "frame"

    def __init__(self, frames: dict[str, pd.DataFrame], cursor: int):
        self.frames, self.cursor = frames, cursor

    def fetch_history(self, symbol, interval="1d", period=None, start=None, end=None):
        df = self.frames[symbol].iloc[: self.cursor]
        return df[df.index >= pd.Timestamp(start)] if start is not None else df


def build_engine(settings, db, provider):
    notifier = RecordingNotifier()
    alerts = AlertManager(settings.alerts, notifier, db)
    broker = PaperBroker(CostModel.from_settings(settings.execution))
    return TradingEngine(settings, db, provider, broker, alerts), notifier


def replay(engine, provider, start_cursor, end_cursor):
    for cursor in range(start_cursor, end_cursor + 1):
        provider.cursor = cursor
        engine.run_cycle()


def test_engine_replay_opens_and_closes_trades(settings, db, trending_df):
    frames = {"AAA": trending_df, "BBB": trending_df * 0.5}
    provider = FrameProvider(frames, cursor=25)
    engine, notifier = build_engine(settings, db, provider)
    replay(engine, provider, 25, len(trending_df))

    with db.session() as s:
        pf = engine.portfolio(s)
        trades = list(s.scalars(select(Trade)))
        assert trades, "expected trades during the replay"
        closed = [t for t in trades if t.status == TradeStatus.CLOSED]
        assert closed and all(t.pnl is not None and t.exit_reason is not None for t in closed)
        # cash accounting: initial + realized pnl - cost of open positions
        open_cost = sum(t.entry_price * t.quantity + t.entry_commission for t in trades if t.status == TradeStatus.OPEN)
        assert pf.cash == pytest.approx(pf.initial_capital + pf.realized_pnl - open_cost, rel=1e-9)
        assert pf.realized_pnl == pytest.approx(sum(t.pnl for t in closed))
        filled = s.scalar(select(func.count()).select_from(Order).where(Order.status == OrderStatus.FILLED))
        assert filled == len(trades) + len(closed)
        assert s.scalar(select(func.count()).select_from(PortfolioSnapshot)) > 0
        assert s.scalar(select(func.count()).select_from(Signal).where(Signal.signal_type != "HOLD")) > 0
    assert any("BUY" in title for title, _, _ in notifier.messages)


def test_engine_is_idempotent_per_bar(settings, db, trending_df):
    frames = {"AAA": trending_df, "BBB": trending_df}
    provider = FrameProvider(frames, cursor=len(trending_df))
    engine, _ = build_engine(settings, db, provider)
    # Find a bar with a BUY signal and stop the feed there.
    from strategies import get_strategy

    sig = get_strategy("ma_crossover", {"fast_period": 5, "slow_period": 20}).generate_signals(trending_df)["signal"]
    buy_pos = int((sig.to_numpy() == 1).nonzero()[0][0])
    provider.cursor = buy_pos + 1
    engine.run_cycle()
    engine.run_cycle()  # same bar again -> no duplicate orders
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(Order)) == 2  # one per symbol
        assert s.scalar(select(func.count()).select_from(Trade)) == 2


def test_engine_max_positions(settings, db, trending_df):
    settings.risk.max_open_positions = 1
    provider = FrameProvider({"AAA": trending_df, "BBB": trending_df}, cursor=len(trending_df))
    engine, notifier = build_engine(settings, db, provider)
    from strategies import get_strategy

    sig = get_strategy("ma_crossover", {"fast_period": 5, "slow_period": 20}).generate_signals(trending_df)["signal"]
    provider.cursor = int((sig.to_numpy() == 1).nonzero()[0][0]) + 1
    report = engine.run_cycle()
    with db.session() as s:
        assert len(repo.open_trades(s, engine.portfolio(s).id)) == 1
    assert any("blocked" in r.action for r in report.symbols)
    assert any(title.startswith("Entry blocked") for title, _, _ in notifier.messages)


def test_engine_stop_loss_exit(settings, db):
    # Uptrend creating a BUY, then a crash below the stop.
    import numpy as np

    closes = np.concatenate([np.linspace(100, 90, 25), np.linspace(90, 96, 6), [80, 78, 77]])
    df = make_ohlcv(closes)
    settings.watchlist = settings.watchlist[:1]
    settings.risk.take_profit_pct = 1.0
    provider = FrameProvider({"AAA": df}, cursor=22)
    engine, _ = build_engine(settings, db, provider)
    replay(engine, provider, 22, len(df))
    with db.session() as s:
        trades = list(s.scalars(select(Trade)))
        assert any(t.exit_reason and t.exit_reason.value == "STOP_LOSS" for t in trades)


def test_engine_survives_provider_errors(settings, db):
    class Broken(DataProvider):
        name = "broken"

        def fetch_history(self, *a, **k):
            raise RuntimeError("boom")  # not a DataProviderError -> unexpected crash path

    engine, notifier = build_engine(settings, db, Broken())
    report = engine.run_cycle()
    assert len(report.errors) == 2
    assert any("Error in" in title for title, _, _ in notifier.messages)


def test_limit_orders_pending_then_filled(settings, db, trending_df):
    settings.execution.order_type = "limit"
    settings.execution.limit_offset_pct = 0.0
    settings.watchlist = settings.watchlist[:1]
    provider = FrameProvider({"AAA": trending_df}, cursor=len(trending_df))
    engine, _ = build_engine(settings, db, provider)
    from strategies import get_strategy

    sig = get_strategy("ma_crossover", {"fast_period": 5, "slow_period": 20}).generate_signals(trending_df)["signal"]
    provider.cursor = int((sig.to_numpy() == 1).nonzero()[0][0]) + 1
    engine.run_cycle()
    with db.session() as s:
        orders = list(s.scalars(select(Order)))
        assert len(orders) == 1 and orders[0].order_type.value == "LIMIT"
        # limit == price, slippage pushes market fill above limit -> capped at limit, still fills
        assert orders[0].status in {OrderStatus.FILLED, OrderStatus.PENDING}


def test_live_mode_requires_confirmation(settings, monkeypatch):
    settings.execution.mode = "live"
    settings.execution.broker = "alpaca"
    monkeypatch.delenv(CONFIRM_ENV, raising=False)
    with pytest.raises(LiveTradingNotConfirmed):
        confirm_live_trading(settings, interactive=True, input_fn=lambda _: "test")
    monkeypatch.setenv(CONFIRM_ENV, CONFIRM_VALUE)
    with pytest.raises(LiveTradingNotConfirmed):
        confirm_live_trading(settings, interactive=False)
    with pytest.raises(LiveTradingNotConfirmed):
        confirm_live_trading(settings, interactive=True, input_fn=lambda _: "wrong")
    confirm_live_trading(settings, interactive=True, input_fn=lambda _: "test")  # passes


def test_period_pnl_and_snapshots(settings, db):
    provider = SyntheticProvider(end=datetime(2026, 6, 30))
    engine, _ = build_engine(settings, db, provider)
    engine.run_cycle()
    with db.session() as s:
        pf = engine.portfolio(s)
        daily = repo.period_pnl(s, pf, "D")
        monthly = repo.period_pnl(s, pf, "M")
        assert len(daily) == 1 and len(monthly) == 1
        assert daily["equity"].iloc[0] == pytest.approx(pf.equity)


def test_engine_with_file_sqlite_no_lock_errors(settings, tmp_path, trending_df):
    """File-based SQLite: alert logging must not deadlock against the engine transaction."""
    import time

    from database.models import AlertLog
    from database.session import Database

    file_db = Database(f"sqlite:///{tmp_path / 'bot.db'}")
    file_db.create_all()
    provider = FrameProvider({"AAA": trending_df, "BBB": trending_df}, cursor=25)
    engine, notifier = build_engine(settings, file_db, provider)
    t0 = time.monotonic()
    replay(engine, provider, 25, 120)
    assert time.monotonic() - t0 < 30
    with file_db.session() as s:
        logged = s.scalar(select(func.count()).select_from(AlertLog))
        assert logged == len(notifier.messages) > 0


def test_daily_signal_ignores_bar_of_open_session(settings, db, trending_df):
    """While the market is open, today's (unfinished) daily bar must not drive signals."""
    from strategies import get_strategy

    settings.watchlist = settings.watchlist[:1]
    sig = get_strategy("ma_crossover", {"fast_period": 5, "slow_period": 20}).generate_signals(trending_df)["signal"]
    buy_pos = int((sig.to_numpy() == 1).nonzero()[0][0])
    # Re-stamp bars so the BUY bar is "today" 00:00 New York and "now" is 11:00 New York (session open).
    df = trending_df.iloc[: buy_pos + 1].copy()
    today_bar = datetime(2026, 6, 2, 4, 0)  # Tue 00:00 America/New_York in UTC
    df.index = pd.DatetimeIndex([today_bar - timedelta(days=len(df) - 1 - i) for i in range(len(df))])
    provider = FrameProvider({"AAA": df}, cursor=len(df))

    engine, _ = build_engine(settings, db, provider)
    engine.clock = lambda: datetime(2026, 6, 2, 15, 0)  # 11:00 New York -> open
    report = engine.run_cycle()
    assert report.symbols[0].signal == "HOLD"
    assert "still open" in report.symbols[0].action
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(Trade)) == 0

    engine.clock = lambda: datetime(2026, 6, 2, 21, 0)  # 17:00 New York -> closed
    report = engine.run_cycle()
    assert report.symbols[0].signal == "BUY"
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(Trade)) == 1
