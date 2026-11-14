"""Weekly strategy review (walk-forward + change rules) and the emergency brake. Paper only, all files in tmp_path."""

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import backtesting.review as review
from backtesting.review import (
    SCORE_FORMULA, WindowScore, beats_by, decide_change, evaluate_window, next_review_at, review_symbol,
    run_strategy_review, score_of, split_walk_forward,
)
from config.settings import EmergencyBrakeSettings, StrategyReviewSettings
from dashboard.app import create_app
from database import repository as repo
from database.models import Order, OrderStatus, Portfolio, Trade, TradeStatus
from monitoring.review_store import ReviewStore, effective_strategy
from risk.brake import EmergencyBrake
from tests.conftest import make_ohlcv
from tests.test_engine import FrameProvider, build_engine, replay

NOW = datetime(2026, 11, 14, 3, 0, tzinfo=timezone.utc)  # Saturday 10:00 Thai time
CFG = StrategyReviewSettings()


def ws(name, score, trades=5):
    return WindowScore(name, ret=score, max_dd=0.0, trades=trades, score=score)


def scores(**kw):
    """scores(ma_crossover=0.05, rsi=(0.07, 2)) -> {name: WindowScore}"""
    return {n: ws(n, *(v if isinstance(v, tuple) else (v,))) for n, v in kw.items()}


def decide(sc, *, current="ma_crossover", cfg=CFG, has_position=False, last_changed=None):
    return decide_change("AAA", current, sc, cfg, has_position=has_position, last_changed=last_changed, now=NOW)


def wave_df(n=620, seed=1):
    rng = np.random.default_rng(seed)
    i = np.arange(n)
    closes = 100 + 18 * np.sin(i / 22) + 0.03 * i + rng.normal(0, 0.8, n)
    return make_ohlcv(closes, start="2024-01-01")


# ====================================================================== score + config
def test_score_formula_and_negative_returns():
    assert score_of(0.10, 0.10) == pytest.approx(0.10 / 1.10)
    assert score_of(-0.10, 0.10) == pytest.approx(-0.10 * 1.10)  # deeper drawdown never improves a loss
    assert "(1 + max_drawdown)" in SCORE_FORMULA


def test_review_defaults_match_the_brief():
    c = StrategyReviewSettings()
    assert (c.day_of_week, c.hour, c.minute, c.timezone) == ("sat", 10, 0, "Asia/Bangkok")
    assert (c.train_months, c.test_months, c.min_improvement_pct, c.min_test_trades, c.cooldown_weeks) == (12, 6, 0.2, 3, 4)
    b = EmergencyBrakeSettings()
    assert (b.trigger_drawdown_pct, b.release_drawdown_pct) == (0.10, 0.05)
    with pytest.raises(ValueError):
        EmergencyBrakeSettings(trigger_drawdown_pct=0.05, release_drawdown_pct=0.10)


def test_next_review_is_saturday_10_bangkok():
    nxt = next_review_at(CFG, datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc))  # Wednesday
    local = nxt.astimezone(timezone(timedelta(hours=7)))
    assert (local.weekday(), local.hour, local.minute) == (5, 10, 0)


def test_scheduler_has_review_job(settings, db, monkeypatch):
    from scheduler.jobs import build_scheduler

    engine, _ = build_engine(settings, db, FrameProvider({"AAA": wave_df(40)}, 40))
    jobs = {j.id: j for j in build_scheduler(engine).get_jobs()}
    assert "strategy_review" in jobs
    settings.strategy_review.enabled = False
    assert "strategy_review" not in {j.id for j in build_scheduler(engine).get_jobs()}


# ====================================================================== walk-forward: no peeking
def test_split_train_never_contains_test_bars():
    df = wave_df()
    sp = split_walk_forward(df, 12, 6)
    assert sp.test_start == df.index[-1] - pd.DateOffset(months=6)
    assert sp.train_start == sp.test_start - pd.DateOffset(months=12)
    assert sp.train_df.index.max() < sp.test_start <= sp.full_df.index[-1]
    assert (sp.full_df.index >= sp.test_start).sum() > 0 and (sp.train_df.index >= sp.test_start).sum() == 0


def test_train_result_is_independent_of_test_period_data(settings):
    item = settings.watchlist[0]
    store = ReviewStore(settings.strategy_review.overrides_file, settings.strategy_review.history_file)
    a = wave_df()
    sp = split_walk_forward(a, 12, 6)
    b = a.copy()
    mask = b.index >= sp.test_start  # scramble ONLY the exam period
    b.loc[mask, ["open", "high", "low", "close"]] = b.loc[mask, ["open", "high", "low", "close"]].to_numpy()[::-1] * 1.7
    ra = review_symbol(item, a, settings, store, has_position=False, now=NOW)
    rb = review_symbol(item, b, settings, store, has_position=False, now=NOW)
    for name in ra.train:
        assert ra.train[name].to_dict() == rb.train[name].to_dict(), f"train leaked test data for {name}"
    assert any(ra.test[n].to_dict() != rb.test[n].to_dict() for n in ra.test)  # sanity: test did change
    assert ra.windows["train"].endswith(f"{sp.test_start:%Y-%m-%d}")


def test_window_metrics_only_count_the_window(settings):
    df = wave_df()
    sp = split_walk_forward(df, 12, 6)
    full = evaluate_window(df, "ma_crossover", "AAA", settings, "US", df.index[0])
    part = evaluate_window(df, "ma_crossover", "AAA", settings, "US", sp.test_start)
    assert part.error is None and full.error is None
    assert part.trades <= full.trades
    assert part.max_dd is not None and part.max_dd >= 0
    assert part.score == pytest.approx(score_of(part.ret, part.max_dd))


def test_review_symbol_scores_all_five_strategies(settings):
    item = settings.watchlist[0]
    store = ReviewStore(settings.strategy_review.overrides_file, settings.strategy_review.history_file)
    rev = review_symbol(item, wave_df(), settings, store, has_position=False, now=NOW)
    assert set(rev.test) == set(settings.strategy_review.strategies) == set(rev.train)
    assert rev.decision is not None and rev.windows["test"].endswith(f"{wave_df().index[-1]:%Y-%m-%d}")


def test_review_symbol_skips_when_not_enough_data(settings):
    store = ReviewStore(settings.strategy_review.overrides_file, settings.strategy_review.history_file)
    rev = review_symbol(settings.watchlist[0], wave_df(10), settings, store, has_position=False, now=NOW)
    assert rev.skipped and rev.decision is None


# ====================================================================== change rules
def test_change_when_every_rule_passes():
    d = decide(scores(ma_crossover=0.05, rsi=0.07))
    assert d.changed and d.candidate == "rsi" and d.code == "passed"


@pytest.mark.parametrize("cand,expected", [(0.0599, False), (0.06, True), (0.08, True)])
def test_improvement_threshold_is_20_percent(cand, expected):
    assert decide(scores(ma_crossover=0.05, rsi=cand)).changed is expected


def test_improvement_threshold_follows_config():
    cfg = StrategyReviewSettings(min_improvement_pct=0.5)
    assert not decide(scores(ma_crossover=0.05, rsi=0.07), cfg=cfg).changed
    assert decide(scores(ma_crossover=0.05, rsi=0.075), cfg=cfg).changed


def test_improvement_with_zero_or_negative_current_score():
    assert decide(scores(ma_crossover=0.0, rsi=0.01)).changed
    assert not decide(scores(ma_crossover=0.0, rsi=0.0)).changed
    assert not decide(scores(ma_crossover=-0.02, rsi=-0.0195)).changed  # < 20% of |current|
    assert beats_by(-0.01, -0.02, 0.2)


@pytest.mark.parametrize("trades,expected", [(2, False), (3, True), (10, True)])
def test_candidate_needs_min_test_trades(trades, expected):
    d = decide(scores(ma_crossover=0.05, rsi=(0.20, trades)))
    assert d.changed is expected
    if not expected:
        assert d.code == "too_few_trades"


def test_few_trade_winner_is_skipped_for_eligible_runner_up():
    d = decide(scores(ma_crossover=0.05, rsi=(0.30, 1), bollinger=(0.10, 4)))
    assert d.changed and d.candidate == "bollinger"


def test_trade_threshold_follows_config():
    cfg = StrategyReviewSettings(min_test_trades=6)
    assert not decide(scores(ma_crossover=0.05, rsi=(0.2, 5)), cfg=cfg).changed


def test_cooldown_four_weeks():
    sc = scores(ma_crossover=0.05, rsi=0.2)
    assert decide(sc, last_changed=NOW - timedelta(days=27)).code == "cooldown"
    assert decide(sc, last_changed=NOW - timedelta(days=28)).changed
    assert decide(sc, last_changed=None).changed
    assert decide(sc, last_changed=NOW - timedelta(days=27), cfg=StrategyReviewSettings(cooldown_weeks=2)).changed


def test_open_position_makes_it_pending_not_changed():
    d = decide(scores(ma_crossover=0.05, rsi=0.2), has_position=True)
    assert d.action == "pending" and not d.changed and d.candidate == "rsi"


def test_position_does_not_matter_when_rules_fail():
    d = decide(scores(ma_crossover=0.05, rsi=0.051), has_position=True)
    assert d.action == "keep"  # nothing to wait for


def test_no_current_score_means_no_change():
    sc = scores(rsi=0.5)
    sc["ma_crossover"] = WindowScore("ma_crossover", error="not enough bars")
    assert decide(sc).action == "keep"


# ====================================================================== run_strategy_review (stored, notified)
def fake_scores(table, calls=None):
    def fake(df, strategy, symbol, settings, market, window_start):
        return table[symbol][strategy]
    return fake


def full_table(**per_symbol):
    """full_table(AAA={"rsi": (0.05, 6), "ma_crossover": 0.02}) -> {symbol: {strategy: WindowScore}}; others -1% x5 trades."""
    names = ["ma_crossover", "rsi", "bollinger", "breakout", "momentum"]
    out = {}
    for sym, vals in per_symbol.items():
        out[sym] = {}
        for n in names:
            v = vals.get(n, (-0.01, 5))
            out[sym][n] = ws(n, *(v if isinstance(v, tuple) else (v,)))
    return out


def seeded_engine(settings, db, trending_df, buy=False):
    frames = {"AAA": trending_df, "BBB": trending_df}
    provider = FrameProvider(frames, cursor=len(trending_df))
    engine, notifier = build_engine(settings, db, provider)
    if buy:
        from strategies import get_strategy

        sig = get_strategy("ma_crossover", {"fast_period": 5, "slow_period": 20}).generate_signals(trending_df)["signal"]
        provider.cursor = int((sig.to_numpy() == 1).nonzero()[0][0]) + 1
    engine.run_cycle()  # also stores the bars in the DB
    return engine, notifier


def test_review_saves_override_history_and_notifies(settings, db, trending_df, monkeypatch):
    engine, notifier = seeded_engine(settings, db, trending_df)
    notifier.messages.clear()
    monkeypatch.setattr(review, "evaluate_window", fake_scores(full_table(
        AAA={"ma_crossover": 0.02, "rsi": (0.05, 6)}, BBB={"ma_crossover": 0.03, "rsi": 0.031})))
    before_cfg = settings.model_dump()

    result = run_strategy_review(settings, db, engine.alerts, engine.review_store, now=NOW)

    assert [r.symbol for r in result.changed] == ["AAA"]
    store = engine.review_store
    assert store.override_for("AAA") == "rsi" and store.override_for("BBB") is None
    hist = store.history()
    assert len(hist) == 1
    h = hist[0]
    assert (h["symbol"], h["from"], h["to"]) == ("AAA", "ma_crossover", "rsi")
    assert h["date"].startswith("2026-11-14") and h["reason"]
    assert set(h["scores"]) == {"ma_crossover", "rsi", "bollinger", "breakout", "momentum"}
    assert h["scores"]["rsi"]["score"] == pytest.approx(0.05)
    assert settings.model_dump() == before_cfg  # config object untouched (and no YAML is written at all)
    # engine now trades AAA with the override, BBB with the default
    items = {i.symbol: i for i in settings.watchlist}
    assert engine._strategy_for(items["AAA"]).name == "rsi" and engine._strategy_for(items["BBB"]).name == "ma_crossover"
    assert effective_strategy(settings, store, items["AAA"]) == "rsi"
    # Discord summary: table + decisions + formula
    title, body, _ = next(m for m in notifier.messages if "สอบกลยุทธ์" in m[0])
    assert "AAA" in body and "BBB" in body and "(1 + max_drawdown)" in body and "เปลี่ยน" in body and "คงเดิม" in body
    last = store.last_review()
    assert last["symbols"][0]["symbol"] == "AAA" and last["next_at"]


def test_review_does_not_switch_while_holding_then_switches_after_close(settings, db, trending_df, monkeypatch):
    engine, _ = seeded_engine(settings, db, trending_df, buy=True)
    with db.session() as s:
        assert repo.open_trades(s, engine.portfolio(s).id), "precondition: AAA is held"
    monkeypatch.setattr(review, "evaluate_window", fake_scores(full_table(
        AAA={"ma_crossover": 0.02, "rsi": (0.05, 6)}, BBB={"ma_crossover": 0.02, "rsi": (0.05, 6)})))

    r1 = run_strategy_review(settings, db, engine.alerts, engine.review_store, now=NOW)
    store = engine.review_store
    assert r1.changed == [] and store.override_for("AAA") is None and store.history() == []
    assert set(store.pending()) == {"AAA", "BBB"} and store.pending()["AAA"]["to"] == "rsi"
    assert {r["action"] for r in store.last_review()["symbols"]} == {"pending"}

    engine.close_position("AAA")  # position closed -> next review may switch AAA only
    r2 = run_strategy_review(settings, db, engine.alerts, engine.review_store, now=NOW + timedelta(days=7))
    assert [r.symbol for r in r2.changed] == ["AAA"]
    assert store.override_for("AAA") == "rsi" and store.override_for("BBB") is None
    assert "AAA" not in store.pending() and "BBB" in store.pending()


def test_pending_buy_order_also_counts_as_holding(settings, db, trending_df):
    engine, _ = seeded_engine(settings, db, trending_df)
    assert review.has_open_exposure(db, settings, "AAA") is False
    with db.session() as s:
        pf = engine.portfolio(s)
        stock = repo.get_stock(s, "AAA")
        from database.models import OrderSide, OrderType

        s.add(Order(portfolio_id=pf.id, stock_id=stock.id, mode=pf.mode, side=OrderSide.BUY, order_type=OrderType.LIMIT,
                    quantity=1, status=OrderStatus.PENDING, broker="paper", created_at=datetime(2026, 1, 1)))
    assert review.has_open_exposure(db, settings, "AAA") is True


def test_review_respects_four_week_cooldown_across_runs(settings, db, trending_df, monkeypatch):
    engine, _ = seeded_engine(settings, db, trending_df)
    store = engine.review_store
    monkeypatch.setattr(review, "evaluate_window", fake_scores(full_table(
        AAA={"ma_crossover": 0.02, "rsi": (0.05, 6)}, BBB={"ma_crossover": 0.5})))
    run_strategy_review(settings, db, None, store, now=NOW)
    assert store.override_for("AAA") == "rsi"
    # next week "bollinger" looks even better, but AAA changed 7 days ago
    monkeypatch.setattr(review, "evaluate_window", fake_scores(full_table(
        AAA={"ma_crossover": 0.02, "rsi": 0.05, "bollinger": (0.2, 9)}, BBB={"ma_crossover": 0.5})))
    r = run_strategy_review(settings, db, None, store, now=NOW + timedelta(days=7))
    assert r.changed == [] and store.override_for("AAA") == "rsi"
    assert store.last_review()["symbols"][0]["code"] == "cooldown"
    r = run_strategy_review(settings, db, None, store, now=NOW + timedelta(days=28))
    assert [x.symbol for x in r.changed] == ["AAA"] and store.override_for("AAA") == "bollinger"
    assert [h["to"] for h in store.history()] == ["rsi", "bollinger"]


def test_review_skips_symbol_without_data_and_survives_errors(settings, db, monkeypatch):
    store = ReviewStore.from_settings(settings)
    r = run_strategy_review(settings, db, None, store, now=NOW)
    assert all(x.skipped for x in r.reviews) and store.history() == []
    assert not store.is_running()


def test_review_refuses_to_run_twice_at_once(settings, db):
    store = ReviewStore.from_settings(settings)
    assert store.try_start(NOW)
    assert run_strategy_review(settings, db, None, store, now=NOW + timedelta(minutes=1)) is None
    store.finish()
    assert run_strategy_review(settings, db, None, store, now=NOW + timedelta(minutes=2)) is not None


def test_json_files_are_written_atomically(settings, tmp_path):
    store = ReviewStore.from_settings(settings)
    store.set_override("AAA", "rsi", previous="ma_crossover", changed_at=NOW)
    store.append_history({"symbol": "AAA"})
    assert not list(tmp_path.glob(".*.tmp"))  # temp files are renamed away
    assert (tmp_path / "overrides.json").exists() and (tmp_path / "history.json").exists()


# ====================================================================== emergency brake
def make_brake(tmp_path, **kw):
    return EmergencyBrake(EmergencyBrakeSettings(**kw), tmp_path / "b.json")


def test_brake_turns_on_at_10_percent(tmp_path):
    b = make_brake(tmp_path)
    assert b.update(0.0999) is None and not b.active
    assert b.update(0.10) == "ON" and b.active
    assert b.update(0.12) is None and b.active  # no repeated event


def test_brake_releases_below_5_percent_only(tmp_path):
    b = make_brake(tmp_path)
    b.update(0.11)
    assert b.update(0.07) is None and b.active  # between the levels: stays on
    assert b.update(0.05) is None and b.active  # exactly 5% is not "< 5%"
    assert b.update(0.0499) == "OFF" and not b.active
    assert b.update(0.08) is None  # and does not re-trigger below 10%


def test_brake_levels_come_from_config(tmp_path):
    b = make_brake(tmp_path, trigger_drawdown_pct=0.20, release_drawdown_pct=0.15)
    assert b.update(0.15) is None
    assert b.update(0.20) == "ON" and b.update(0.149) == "OFF"


def test_brake_manual_resume_and_rearm(tmp_path):
    b = make_brake(tmp_path)
    assert b.resume() is False  # nothing to release
    b.update(0.12)
    assert b.resume(0.12) is True and not b.active
    assert b.update(0.12) is None and not b.active  # no instant re-trigger after a manual release
    assert b.update(0.04) is None  # recovered -> re-armed
    assert b.update(0.10) == "ON"


def test_brake_state_is_shared_through_the_file_and_disabled_flag(tmp_path):
    a, other = make_brake(tmp_path), make_brake(tmp_path)
    a.update(0.2)
    assert other.active and other.state()["since"]
    assert not (tmp_path / "b.json").read_text().startswith("{}")
    off = make_brake(tmp_path, enabled=False)
    assert off.update(0.5) is None and not off.active


def drawdown_to(db, settings, pct):
    """Force the paper portfolio's peak so that its drawdown is ``pct`` (fraction)."""
    with db.session() as s:
        pf = s.scalar(select(Portfolio).where(Portfolio.name == settings.execution.portfolio_name))
        pf.peak_equity = pf.equity / (1 - pct)


def test_engine_brake_blocks_new_entries_and_alerts(settings, db, trending_df):
    provider = FrameProvider({"AAA": trending_df, "BBB": trending_df}, cursor=len(trending_df))
    engine, notifier = build_engine(settings, db, provider)
    from strategies import get_strategy

    sig = get_strategy("ma_crossover", {"fast_period": 5, "slow_period": 20}).generate_signals(trending_df)["signal"]
    provider.cursor = int((sig.to_numpy() == 1).nonzero()[0][0]) + 1
    with db.session() as s:
        engine.portfolio(s)
    drawdown_to(db, settings, 0.12)

    report = engine.run_cycle()

    assert engine.brake.active
    assert any("emergency brake" in r.action for r in report.symbols)
    with db.session() as s:
        assert not list(s.scalars(select(Trade)))  # nothing opened
    titles = [t for t, _, lvl in notifier.messages if "เบรกฉุกเฉิน" in t]
    assert len(titles) == 1  # alerted once, not on every blocked symbol
    engine.run_cycle()
    assert len([t for t, _, _ in notifier.messages if "เบรกฉุกเฉิน" in t]) == 1


def test_engine_brake_releases_automatically(settings, db, trending_df):
    provider = FrameProvider({"AAA": trending_df, "BBB": trending_df}, cursor=len(trending_df))
    engine, notifier = build_engine(settings, db, provider)
    from strategies import get_strategy

    sig = get_strategy("ma_crossover", {"fast_period": 5, "slow_period": 20}).generate_signals(trending_df)["signal"]
    provider.cursor = int((sig.to_numpy() == 1).nonzero()[0][0]) + 1
    with db.session() as s:
        engine.portfolio(s)
    drawdown_to(db, settings, 0.12)
    engine.run_cycle()
    assert engine.brake.active
    drawdown_to(db, settings, 0.01)  # recovered
    report = engine.run_cycle()
    assert not engine.brake.active
    assert any("ปลดเบรก" in t for t, _, _ in notifier.messages)
    assert not any("emergency brake" in r.action for r in report.symbols)  # entries are no longer refused by it
    # (the BUY signal of the braked bar was already consumed, as with any blocked entry - a later signal trades)


def test_engine_brake_still_lets_stop_loss_sell(settings, db):
    closes = np.concatenate([np.linspace(100, 90, 25), np.linspace(90, 96, 6), [80, 78, 77]])
    df = make_ohlcv(closes)
    settings.watchlist = settings.watchlist[:1]
    settings.risk.take_profit_pct = 1.0
    provider = FrameProvider({"AAA": df}, cursor=22)
    engine, _ = build_engine(settings, db, provider)
    for cursor in range(22, len(df) + 1):
        provider.cursor = cursor
        engine.run_cycle()
        with db.session() as s:
            if repo.open_trades(s, engine.portfolio(s).id):
                break
    else:
        pytest.fail("test data never opened a position")
    drawdown_to(db, settings, 0.15)  # big drawdown while holding
    for cursor in range(cursor + 1, len(df) + 1):
        provider.cursor = cursor
        engine.run_cycle()
    assert engine.brake.active
    with db.session() as s:
        trades = list(s.scalars(select(Trade)))
        assert any(t.status == TradeStatus.CLOSED and t.exit_reason.value == "STOP_LOSS" for t in trades)


def test_engine_brake_cancels_pending_buy_orders(settings, db, trending_df):
    settings.execution.order_type = "limit"
    settings.watchlist = settings.watchlist[:1]
    provider = FrameProvider({"AAA": trending_df}, cursor=len(trending_df))
    engine, _ = build_engine(settings, db, provider)
    with db.session() as s:
        pf = engine.portfolio(s)
        stock = repo.get_or_create_stock(s, "AAA")
        from database.models import OrderSide, OrderType

        s.add(Order(portfolio_id=pf.id, stock_id=stock.id, mode=pf.mode, side=OrderSide.BUY, order_type=OrderType.LIMIT,
                    quantity=1, limit_price=1.0, reference_price=100.0, status=OrderStatus.PENDING, broker="paper",
                    created_at=engine.clock()))
    engine.brake.update(0.2)
    drawdown_to(db, settings, 0.2)
    engine.run_cycle()
    with db.session() as s:
        o = s.scalar(select(Order))
        assert o.status == OrderStatus.CANCELLED and "brake" in o.message


# ====================================================================== /monitor
def make_client(settings, db, tmp_path, run_review=None):
    store, brake = ReviewStore.from_settings(settings), EmergencyBrake.from_settings(settings)
    app = create_app(settings, db, run_cycle=lambda: None, status_file=tmp_path / "status.json",
                     run_review=run_review or (lambda: None), review_store=store, brake=brake)
    return TestClient(app), store, brake


def test_monitor_shows_strategy_review_and_brake(settings, db, tmp_path):
    from monitoring.status import StatusWriter

    client, store, brake = make_client(settings, db, tmp_path)
    w = StatusWriter(tmp_path / "status.json")
    w.set_watchlist_order(["AAA", "BBB"])
    for sym in ("AAA", "BBB"):
        w.update_symbol({"symbol": sym, "signal": "HOLD", "price": 1.0, "percent_change": 0.0, "strategy": "ma_crossover",
                         "action": "", "blocked": False, "error": None})
    j = client.get("/api/monitor/status").json()
    assert j["brake"]["active"] is False and j["review"]["last"] is None and j["review"]["next_at"]
    assert j["review"]["can_run"] is True
    store.set_override("AAA", "rsi", previous="ma_crossover", changed_at=NOW)
    store.set_pending("BBB", {"to": "bollinger"})
    brake.update(0.2)
    j = client.get("/api/monitor/status").json()
    by = {x["symbol"]: x for x in j["watchlist"]}
    assert by["AAA"]["strategy"] == "rsi" and by["BBB"]["strategy"] == "ma_crossover"
    assert by["BBB"]["pending_strategy"] == "bollinger"
    assert j["brake"]["active"] is True
    html = client.get("/monitor").text
    assert "เบรกฉุกเฉิน" in html and "สอบกลยุทธ์ตอนนี้" in html and "สอบกลยุทธ์ล่าสุด" in html


def test_review_now_button_runs_once_and_blocks_double_click(settings, db, tmp_path):
    import threading

    started, release, calls = threading.Event(), threading.Event(), []

    def slow_review():
        calls.append(1)
        started.set()
        release.wait(5)

    client, _, _ = make_client(settings, db, tmp_path, run_review=slow_review)
    assert client.post("/api/monitor/review-now").status_code == 202
    assert started.wait(5)
    assert client.post("/api/monitor/review-now").status_code == 409  # double click
    assert client.get("/api/monitor/status").json()["review"]["can_run"] is False
    release.set()
    for _ in range(100):
        if client.get("/api/monitor/status").json()["review"]["can_run"]:
            break
        threading.Event().wait(0.05)
    assert calls == [1]
    assert client.post("/api/monitor/review-now").status_code == 202


def test_review_now_is_refused_for_live_config(settings, db, tmp_path):
    client, _, _ = make_client(settings, db, tmp_path)
    settings.execution.mode, settings.execution.broker = "live", "alpaca"
    assert client.post("/api/monitor/review-now").status_code == 403
