import math

import numpy as np
import pandas as pd
import pytest

from analysis.metrics import compute_metrics, max_drawdown, profit_factor, sharpe_ratio
from backtesting.engine import BacktestConfig, Backtester
from config.settings import RiskSettings
from execution.costs import CostModel
from strategies import get_strategy
from tests.conftest import make_ohlcv


def test_max_drawdown():
    eq = pd.Series([100, 120, 90, 130, 117])
    assert max_drawdown(eq) == pytest.approx(0.25)


def test_profit_factor():
    assert profit_factor([100, -50, 50]) == pytest.approx(3.0)
    assert math.isinf(profit_factor([10, 20]))
    assert profit_factor([]) == 0.0


def test_sharpe_sign():
    rng = np.random.default_rng(0)
    assert sharpe_ratio(pd.Series(rng.normal(0.001, 0.01, 500))) > 0
    assert sharpe_ratio(pd.Series(rng.normal(-0.002, 0.01, 500))) < 0
    assert sharpe_ratio(pd.Series([0.01, 0.01, 0.01])) == 0.0  # zero variance


def test_compute_metrics_values():
    eq = pd.Series([100.0, 110.0, 99.0, 121.0])
    m = compute_metrics(eq, [10, -11, 22], periods_per_year=252)
    assert m.total_return_pct == pytest.approx(21.0)
    assert m.num_trades == 3 and m.win_rate_pct == pytest.approx(200 / 3)
    assert m.profit_factor == pytest.approx(32 / 11)
    assert m.max_drawdown_pct == pytest.approx(10.0)
    assert m.to_dict()["num_trades"] == 3


def test_backtest_trend_trade_profit(trending_df):
    cfg = BacktestConfig(initial_capital=10_000, costs=CostModel(0, 0, 0, 0),
                         risk=RiskSettings(position_size_pct=1.0, risk_per_trade_pct=None, stop_loss_pct=0.5,
                                           take_profit_pct=5.0, max_drawdown_pct=1.0, max_daily_loss_pct=1.0))
    res = Backtester(get_strategy("ma_crossover", {"fast_period": 5, "slow_period": 20}), cfg).run(trending_df, "T")
    assert res.metrics.num_trades >= 1
    first = res.trades[0]
    # entry happens on the bar AFTER the signal, at its open
    sig_bar = res.signals.index[res.signals["signal"] == 1][0]
    assert first.entry_time > sig_bar.to_pydatetime()
    assert first.entry_price == pytest.approx(trending_df.loc[first.entry_time, "open"])
    assert first.pnl > 0
    assert res.final_equity == pytest.approx(10_000 + sum(t.pnl for t in res.trades))


def test_backtest_costs_reduce_returns(trending_df):
    strat = get_strategy("ma_crossover", {"fast_period": 5, "slow_period": 20})
    free = Backtester(strat, BacktestConfig(costs=CostModel(0, 0, 0, 0))).run(trending_df)
    costly = Backtester(strat, BacktestConfig(costs=CostModel(0.005, 0, 0, 0.005))).run(trending_df)
    assert costly.final_equity < free.final_equity


def test_backtest_stop_loss_triggers():
    closes = np.concatenate([np.linspace(100, 90, 30), np.linspace(90, 96, 6), [80, 78, 77, 76]])
    df = make_ohlcv(closes)
    cfg = BacktestConfig(risk=RiskSettings(stop_loss_pct=0.05, take_profit_pct=1.0))
    res = Backtester(get_strategy("ma_crossover", {"fast_period": 3, "slow_period": 10}), cfg).run(df)
    reasons = {t.exit_reason for t in res.trades}
    assert "STOP_LOSS" in reasons
    for t in res.trades:
        if t.exit_reason == "STOP_LOSS":
            assert t.exit_price <= t.stop_loss * 1.0001 or t.exit_price <= t.entry_price


def test_backtest_drawdown_blocks_entries():
    rng = np.random.default_rng(5)
    df = make_ohlcv(100 * np.exp(np.cumsum(rng.normal(-0.002, 0.03, 500))))
    cfg = BacktestConfig(risk=RiskSettings(position_size_pct=1.0, risk_per_trade_pct=None, max_drawdown_pct=0.02,
                                           stop_loss_pct=0.05))
    res = Backtester(get_strategy("rsi"), cfg).run(df)
    assert any("drawdown" in reason for _, reason in res.blocked_entries)


def test_backtest_too_few_bars():
    with pytest.raises(ValueError):
        Backtester(get_strategy("ma_crossover")).run(make_ohlcv([1, 2, 3]))


def test_lot_size_respected(trending_df):
    res = Backtester(get_strategy("ma_crossover", {"fast_period": 5, "slow_period": 20}),
                     BacktestConfig(lot_size=100)).run(trending_df)
    assert all(t.quantity % 100 == 0 for t in res.trades)
