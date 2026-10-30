import numpy as np
import pytest

from strategies import BUY, SELL, STRATEGIES, StrategyError, get_strategy
from tests.conftest import make_ohlcv


def test_registry_has_all_strategies():
    assert set(STRATEGIES) == {"ma_crossover", "rsi", "bollinger", "breakout", "momentum"}


@pytest.mark.parametrize("name", list(STRATEGIES))
def test_each_strategy_outputs_valid_signals(name):
    rng = np.random.default_rng(7)
    df = make_ohlcv(100 * np.exp(np.cumsum(rng.normal(0, 0.02, 400))))
    out = get_strategy(name).generate_signals(df)
    assert set(out["signal"].unique()) <= {-1, 0, 1}
    assert (out.loc[out["signal"] != 0, "reason"].str.len() > 0).all()
    assert (out.loc[out["signal"] == 0, "reason"] == "").all()
    assert len(out) == len(df)


@pytest.mark.parametrize("name", list(STRATEGIES))
def test_no_lookahead(name):
    """Signals up to bar t must not change when future bars are appended."""
    rng = np.random.default_rng(11)
    df = make_ohlcv(100 * np.exp(np.cumsum(rng.normal(0, 0.02, 300))))
    strat = get_strategy(name)
    full = strat.generate_signals(df)["signal"]
    partial = strat.generate_signals(df.iloc[:200])["signal"]
    assert (full.iloc[:200] == partial).all()


def test_ma_crossover_buy_then_sell(trending_df):
    out = get_strategy("ma_crossover", {"fast_period": 5, "slow_period": 20}).generate_signals(trending_df)
    buys, sells = out.index[out["signal"] == BUY], out.index[out["signal"] == SELL]
    assert len(buys) >= 1 and len(sells) >= 1
    assert buys[0] < sells[-1]
    assert "Golden cross" in out.loc[buys[0], "reason"]


def test_rsi_buys_after_selloff():
    closes = np.concatenate([np.linspace(100, 60, 40), np.linspace(60, 75, 15)])
    out = get_strategy("rsi", {"period": 14}).generate_signals(make_ohlcv(closes))
    assert (out["signal"] == BUY).any()


def test_breakout_on_new_high():
    closes = np.concatenate([np.full(30, 100.0) + np.sin(np.arange(30)), [110.0, 111.0]])
    out = get_strategy("breakout", {"entry_period": 20, "exit_period": 10, "volume_factor": 0}).generate_signals(
        make_ohlcv(closes)
    )
    assert out["signal"].iloc[-2] == BUY
    assert out["signal"].iloc[-1] == 0  # only first breakout bar signals


def test_param_validation_and_coercion():
    with pytest.raises(StrategyError):
        get_strategy("ma_crossover", {"fast_period": 50, "slow_period": 20})
    with pytest.raises(StrategyError):
        get_strategy("rsi", {"oversold": 80, "overbought": 70})
    with pytest.raises(StrategyError):
        get_strategy("rsi", {"unknown": 1})
    with pytest.raises(StrategyError):
        get_strategy("nope")
    s = get_strategy("rsi", {"period": "10", "oversold": 25})
    assert s.params["period"] == 10 and s.params["oversold"] == 25.0
