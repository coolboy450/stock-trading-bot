"""Price guard: every check, boundaries, disabled mode, and engine integration (paper only)."""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from config.settings import PriceGuardSettings, Settings
from database.models import Order, Trade
from risk.price_guard import PriceContext, PriceGuard
from tests.test_engine import FrameProvider, build_engine

NOW = datetime(2026, 11, 13, 12, 0)


def ctx(**kw) -> PriceContext:
    base = dict(market_price=100.0, prev_close=99.0, bar_time=NOW - timedelta(hours=2), now=NOW, interval="1d",
                fetched_this_cycle=True)
    base.update(kw)
    return PriceContext(**base)


def guard(**kw) -> PriceGuard:
    return PriceGuard(PriceGuardSettings(**kw))


def failed_keys(decision) -> set[str]:
    return {c.key for c in decision.failed}


def test_all_pass():
    d = guard().check(side="BUY", quantity=10, order_price=100.5, ctx=ctx())
    assert d.allowed and not d.failed and len(d.checks) == 4


@pytest.mark.parametrize("order_price,ok", [(102.0, True), (98.0, True), (102.5, False), (97.0, False)])
def test_deviation_boundary(order_price, ok):
    d = guard().check(side="BUY", quantity=1, order_price=order_price, ctx=ctx())
    assert d.allowed is ok
    assert failed_keys(d) == (set() if ok else {"deviation"})


def test_deviation_uses_configured_limit():
    assert guard(max_deviation_pct=0.05).check(side="BUY", quantity=1, order_price=104, ctx=ctx()).allowed


def test_stale_when_not_fetched_this_cycle():
    d = guard().check(side="BUY", quantity=1, order_price=100, ctx=ctx(fetched_this_cycle=False))
    assert not d.allowed and failed_keys(d) == {"freshness"}


def test_stale_old_daily_bar_and_boundary():
    g = guard(max_bar_age_days=5)
    assert g.check(side="BUY", quantity=1, order_price=100, ctx=ctx(bar_time=NOW - timedelta(days=5))).allowed
    d = g.check(side="BUY", quantity=1, order_price=100, ctx=ctx(bar_time=NOW - timedelta(days=5, hours=1)))
    assert failed_keys(d) == {"freshness"}


def test_stale_intraday_uses_minutes():
    g = guard(max_bar_age_minutes=30)
    assert g.check(side="BUY", quantity=1, order_price=100,
                   ctx=ctx(interval="5m", bar_time=NOW - timedelta(minutes=30))).allowed
    d = g.check(side="BUY", quantity=1, order_price=100, ctx=ctx(interval="5m", bar_time=NOW - timedelta(minutes=31)))
    assert failed_keys(d) == {"freshness"}


def test_future_bar_is_rejected():
    d = guard().check(side="BUY", quantity=1, order_price=100, ctx=ctx(bar_time=NOW + timedelta(days=1)))
    assert failed_keys(d) == {"freshness"}


def test_not_applicable_fetch_flag_skips_fetch_requirement():
    assert guard().check(side="SELL", quantity=1, order_price=100, ctx=ctx(fetched_this_cycle=None)).allowed


@pytest.mark.parametrize("market,ok", [(120.0, True), (121.0, False), (80.0, True), (79.0, False)])
def test_jump_vs_previous_bar(market, ok):
    d = guard().check(side="BUY", quantity=1, order_price=market, ctx=ctx(market_price=market, prev_close=100.0))
    assert d.allowed is ok
    if not ok:
        assert failed_keys(d) == {"jump"}


def test_jump_skipped_without_previous_close():
    assert guard().check(side="BUY", quantity=1, order_price=100, ctx=ctx(prev_close=None)).allowed


def test_order_value_cap_buy_only():
    g = guard(max_order_value=1000)
    assert g.check(side="BUY", quantity=10, order_price=100, ctx=ctx()).allowed
    assert failed_keys(g.check(side="BUY", quantity=11, order_price=100, ctx=ctx())) == {"order_value"}
    # exits must never be trapped by the cap
    assert g.check(side="SELL", quantity=50, order_price=100, ctx=ctx()).allowed


def test_invalid_prices_fail():
    assert not guard().check(side="BUY", quantity=1, order_price=0, ctx=ctx()).allowed
    assert not guard().check(side="BUY", quantity=1, order_price=100, ctx=ctx(market_price=0)).allowed


def test_multiple_failures_all_reported_with_reason():
    d = guard(max_order_value=10).check(side="BUY", quantity=10, order_price=110, ctx=ctx(fetched_this_cycle=False))
    assert failed_keys(d) == {"deviation", "freshness", "order_value"}
    out = d.to_dict()
    assert out["allowed"] is False and out["reason"] and len(out["checks"]) == 4


def test_disabled_allows_everything():
    d = guard(enabled=False).check(side="BUY", quantity=1e6, order_price=1, ctx=ctx(fetched_this_cycle=False))
    assert d.allowed and d.checks == []


def test_config_defaults_and_validation():
    s = Settings.model_validate({"watchlist": [{"symbol": "AAA", "market": "US"}]})
    assert (s.price_guard.max_deviation_pct, s.price_guard.max_daily_jump_pct) == (0.02, 0.20)
    with pytest.raises(ValueError):
        PriceGuardSettings(max_deviation_pct=0)


# ---------------------------------------------------------------- engine integration
def _buy_engine(settings, db, trending_df):
    from strategies import get_strategy

    provider = FrameProvider({"AAA": trending_df, "BBB": trending_df}, cursor=len(trending_df))
    engine, notifier = build_engine(settings, db, provider)
    sig = get_strategy("ma_crossover", {"fast_period": 5, "slow_period": 20}).generate_signals(trending_df)["signal"]
    provider.cursor = int((sig.to_numpy() == 1).nonzero()[0][0]) + 1  # stop the feed on a BUY bar
    return engine, notifier


def test_engine_cancels_order_alerts_and_records(settings, db, trending_df):
    settings.price_guard.max_order_value = 1.0  # force every BUY to be blocked
    engine, notifier = _buy_engine(settings, db, trending_df)
    engine.run_cycle()

    with db.session() as s:
        orders = list(s.scalars(select(Order)))
        assert orders and all(o.status.value == "REJECTED" for o in orders)
        assert not list(s.scalars(select(Trade)))  # nothing reached the broker
    assert any("ราคาผิดปกติ" in title for title, _, _ in notifier.messages)
    snap = engine.status.snapshot()
    assert snap["guard"]["today"]["blocked"] >= 1
    assert snap["guard"]["recent"][-1]["allowed"] is False
    assert any(w["blocked"] for w in snap["watchlist"])


def test_engine_blocks_stale_data(settings, db, trending_df):
    engine, _ = _buy_engine(settings, db, trending_df)
    real = engine.clock
    engine.clock = lambda: real() + timedelta(days=30)  # data is a month old
    engine.run_cycle()
    snap = engine.status.snapshot()
    assert snap["guard"]["today"]["blocked"] >= 1
    assert any(c["key"] == "freshness" and not c["ok"] for c in snap["guard"]["recent"][-1]["checks"])


def test_engine_allows_when_guard_passes(settings, db, trending_df):
    engine, _ = _buy_engine(settings, db, trending_df)
    engine.run_cycle()
    snap = engine.status.snapshot()
    assert snap["guard"]["today"]["blocked"] == 0
    assert snap["guard"]["last"]["allowed"] is True
    with db.session() as s:
        assert list(s.scalars(select(Trade)))
