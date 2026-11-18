"""ma_crossover ``entry_mode`` = "trend": signals, no double entry, stop-loss cooldown, cross mode unchanged."""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import func, select

from analysis.indicators import ema
from backtesting.engine import Backtester, BacktestConfig
from config.settings import RiskSettings
from database.models import ExitReason, Trade, TradeStatus, TradingMode
from strategies import BUY, SELL, StrategyError, get_strategy
from strategies.base import BaseStrategy, crossed_above, crossed_below
from tests.conftest import make_ohlcv
from tests.test_engine import FrameProvider, build_engine

TREND = {"fast_period": 5, "slow_period": 20, "entry_mode": "trend"}
CROSS = {"fast_period": 5, "slow_period": 20}


def last_signal(closes, params) -> int:
    return int(get_strategy("ma_crossover", params).generate_signals(make_ohlcv(closes))["signal"].iloc[-1])


def test_trend_buys_when_all_conditions_hold():
    closes = 100 + 0.5 * np.arange(80)  # steady uptrend: fast > slow, close > fast, fast rising
    out = get_strategy("ma_crossover", TREND).generate_signals(make_ohlcv(closes))
    last = out.iloc[-1]
    assert last["ma_fast"] > last["ma_slow"] and last["close"] > last["ma_fast"]
    assert last["ma_fast"] > out["ma_fast"].iloc[-6]
    assert last["signal"] == BUY and "Trend entry" in last["reason"]
    assert last_signal(closes, CROSS) == 0  # cross mode: no golden cross on the last bar -> no buy


def test_trend_no_buy_when_fast_below_slow():
    closes = 200 - 0.5 * np.arange(80)  # downtrend
    out = get_strategy("ma_crossover", TREND).generate_signals(make_ohlcv(closes))
    assert out["ma_fast"].iloc[-1] < out["ma_slow"].iloc[-1]
    assert (out["signal"] == BUY).sum() == 0


def test_trend_no_buy_when_close_below_fast():
    closes = np.concatenate([100 + 0.5 * np.arange(79), [125.0]])  # last bar dips under the fast EMA
    out = get_strategy("ma_crossover", TREND).generate_signals(make_ohlcv(closes))
    last = out.iloc[-1]
    assert last["ma_fast"] > last["ma_slow"] and last["close"] < last["ma_fast"]  # preconditions
    assert last["signal"] != BUY
    assert out["signal"].iloc[-2] == BUY  # the bar before still qualified


def test_trend_no_buy_when_fast_not_rising():
    closes = np.concatenate([100 + 1.0 * np.arange(60), 159 - 0.4 * np.arange(8)])
    out = get_strategy("ma_crossover", TREND).generate_signals(make_ohlcv(closes))
    last = out.iloc[-1]
    assert last["ma_fast"] > last["ma_slow"]
    assert last["ma_fast"] <= out["ma_fast"].iloc[-6]
    assert last["signal"] != BUY


def test_trend_exit_is_still_death_cross():
    closes = np.concatenate([100 + 0.5 * np.arange(60), 130 - 1.5 * np.arange(40)])
    out = get_strategy("ma_crossover", TREND).generate_signals(make_ohlcv(closes))
    assert (out["signal"] == SELL).any()
    assert all("Death cross" in r for r in out.loc[out["signal"] == SELL, "reason"])


def test_invalid_entry_mode_rejected():
    with pytest.raises(StrategyError):
        get_strategy("ma_crossover", {**CROSS, "entry_mode": "bogus"})


def test_cross_mode_identical_to_old_behaviour():
    rng = np.random.default_rng(3)
    df = make_ohlcv(100 * np.exp(np.cumsum(rng.normal(0, 0.02, 500))))
    new = get_strategy("ma_crossover").generate_signals(df)
    explicit = get_strategy("ma_crossover", {"entry_mode": "cross"}).generate_signals(df)
    fast, slow = ema(df["close"], 20), ema(df["close"], 50)
    expected = pd.Series(0, index=df.index)
    expected[crossed_above(fast, slow).fillna(False)] = 1
    expected[crossed_below(fast, slow).fillna(False)] = -1
    assert (new["signal"] == expected).all() and (explicit["signal"] == expected).all()
    assert get_strategy("ma_crossover").min_bars == 51 and get_strategy("ma_crossover").entry_cooldown_bars == 0
    assert (new["reason"] == explicit["reason"]).all()
    assert new.loc[new["signal"] == 1, "reason"].str.startswith("Golden cross").all()
    cfg = BacktestConfig()
    a = Backtester(get_strategy("ma_crossover"), cfg).run(df)
    b = Backtester(get_strategy("ma_crossover", {"entry_mode": "cross"}), cfg).run(df)
    assert a.equity_curve.equals(b.equity_curve) and len(a.trades) == len(b.trades) > 0


def test_backtest_never_stacks_positions_in_trend_mode():
    closes = 100 + 0.5 * np.arange(120)  # BUY signal on (almost) every bar
    res = Backtester(get_strategy("ma_crossover", TREND), BacktestConfig(use_stops=False)).run(make_ohlcv(closes))
    assert (res.signals["signal"] == BUY).sum() > 50
    assert len(res.trades) == 1  # one position held to the end, no re-buys
    res2 = Backtester(get_strategy("ma_crossover", TREND), BacktestConfig()).run(make_ohlcv(closes))
    trades = sorted(res2.trades, key=lambda t: t.entry_time)
    assert all(a.exit_time <= b.entry_time for a, b in zip(trades, trades[1:]))


class AlwaysBuy(BaseStrategy):
    """Stub: BUY on every bar, to isolate the stop-loss cooldown."""

    name = "always_buy"
    default_params = {"cooldown": 0}

    @property
    def entry_cooldown_bars(self) -> int:
        return self.params["cooldown"]

    @property
    def min_bars(self) -> int:
        return 1

    def compute_indicators(self, df):
        return df.copy()

    def _signals(self, df):
        return pd.Series(BUY, index=df.index), pd.Series("buy", index=df.index, dtype=object)


def _bars_between_stop_and_reentry(cooldown: int) -> int:
    closes = np.concatenate([np.full(10, 100.0), np.full(40, 90.0)])  # bar 10 gaps down through the 5% stop
    cfg = BacktestConfig(risk=RiskSettings(stop_loss_pct=0.05, take_profit_pct=0.5))
    res = Backtester(AlwaysBuy(cooldown=cooldown), cfg).run(make_ohlcv(closes))
    idx = list(res.signals.index)
    stopped = next(t for t in res.trades if t.exit_reason == "STOP_LOSS")
    nxt = min(t.entry_time for t in res.trades if t.entry_time > stopped.exit_time)
    return idx.index(nxt) - idx.index(stopped.exit_time)


def test_backtest_cooldown_after_stop_loss():
    assert _bars_between_stop_and_reentry(0) == 1  # no cooldown: back in on the very next bar
    assert _bars_between_stop_and_reentry(5) == 6  # 5 signal bars skipped, entry at the open of the 6th


def test_trend_strategy_exposes_cooldown():
    assert get_strategy("ma_crossover", TREND).entry_cooldown_bars == 5
    assert get_strategy("ma_crossover", {**TREND, "cooldown_bars": 8}).entry_cooldown_bars == 8
    assert get_strategy("ma_crossover", CROSS).entry_cooldown_bars == 0


# ------------------------------------------------------------------ live engine
def _trend_settings(settings):
    settings.watchlist = settings.watchlist[:1]
    settings.strategy.params["ma_crossover"] = dict(TREND)
    return settings


def test_engine_trend_mode_no_double_entry(settings, db, trending_df):
    settings = _trend_settings(settings)
    provider = FrameProvider({"AAA": trending_df}, cursor=25)
    engine, _ = build_engine(settings, db, provider)
    for cursor in range(25, len(trending_df) + 1):
        provider.cursor = cursor
        engine.run_cycle()
        with db.session() as s:
            assert s.scalar(select(func.count()).select_from(Trade).where(Trade.status == TradeStatus.OPEN)) <= 1
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(Trade)) >= 1


def test_engine_cooldown_after_stop_loss(settings, db, trending_df):
    settings = _trend_settings(settings)
    provider = FrameProvider({"AAA": trending_df}, cursor=len(trending_df))
    engine, _ = build_engine(settings, db, provider)
    item = settings.watchlist[0]
    strat = engine._strategy_for(item)
    closed = trending_df
    with db.session() as s:
        stock = engine.collector.ensure_stock(s, item)
        pf = engine.portfolio(s)
        assert engine._stop_cooldown_left(s, pf, stock, strat, closed) == 0  # never stopped
        exit_time = closed.index[-3].to_pydatetime()  # stopped 2 bars ago
        s.add(Trade(portfolio_id=pf.id, stock_id=stock.id, strategy_used="ma_crossover", mode=TradingMode.PAPER,
                    status=TradeStatus.CLOSED, quantity=1, entry_price=100, entry_time=exit_time,
                    exit_price=95, exit_time=exit_time, exit_reason=ExitReason.STOP_LOSS, pnl=-5))
        s.flush()
        assert engine._stop_cooldown_left(s, pf, stock, strat, closed) == 3  # 2 bars since -> 3 more
        assert engine._stop_cooldown_left(s, pf, stock, strat, closed.iloc[:-1]) == 4
        cross = get_strategy("ma_crossover", CROSS)
        assert engine._stop_cooldown_left(s, pf, stock, cross, closed) == 0  # cross mode: no cooldown


def test_review_switch_keeps_entry_mode_param(settings):
    """Weekly review only stores a strategy *name* in JSON; the ma_crossover params (entry_mode) stay in config."""
    from monitoring.review_store import ReviewStore

    settings = _trend_settings(settings)
    store = ReviewStore.from_settings(settings)
    store.set_override("AAA", "rsi", previous="ma_crossover", changed_at=datetime(2026, 1, 1))
    store.set_override("AAA", "ma_crossover", previous="rsi", changed_at=datetime(2026, 2, 1))
    assert settings.strategy.params_for("ma_crossover")["entry_mode"] == "trend"
    assert get_strategy("ma_crossover", settings.strategy.params_for("ma_crossover")).params["entry_mode"] == "trend"
