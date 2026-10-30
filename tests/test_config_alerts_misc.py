from datetime import datetime, timezone
from pathlib import Path

import pytest
import requests
from fastapi.testclient import TestClient

from config.settings import EXAMPLE_CONFIG_PATH, MarketSettings, Settings, load_settings
from monitoring.alerts import AlertManager
from monitoring.notifier import DiscordNotifier, MultiNotifier, Notifier, TelegramNotifier, build_notifier
from utils.market_hours import is_market_open
from utils.retry import retry

PROJECT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("name", ["config.example.yaml", "config.thai.example.yaml", "config.demo.yaml"])
def test_example_configs_load(name):
    s = load_settings(PROJECT / "config" / name)
    assert s.execution.mode == "paper" and s.watchlist


def test_env_expansion(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_TOKEN", "abc123")
    cfg = tmp_path / "c.yaml"
    cfg.write_text("alerts:\n  telegram_bot_token: ${MY_TOKEN}\n  telegram_chat_id: ${MISSING_VAR:-}\n"
                   "database:\n  url: ${NOPE:-sqlite:///x.db}\n", encoding="utf-8")
    s = load_settings(cfg, env_file=tmp_path / "none.env")
    assert s.alerts.telegram_bot_token == "abc123"
    assert s.alerts.telegram_chat_id is None
    assert s.database.url == "sqlite:///x.db"


def test_live_with_paper_broker_rejected():
    with pytest.raises(ValueError):
        Settings.model_validate({"execution": {"mode": "live", "broker": "paper"}})


def test_example_config_default_is_paper():
    assert load_settings(EXAMPLE_CONFIG_PATH).execution.mode == "paper"


def test_market_hours():
    us = MarketSettings(timezone="America/New_York", sessions=[("09:30", "16:00")])
    th = MarketSettings(timezone="Asia/Bangkok", sessions=[("10:00", "12:30"), ("14:30", "16:30")],
                        holidays=["2026-06-03"])
    # Tue 2026-06-02 15:00 UTC = 11:00 New York
    assert is_market_open(us, datetime(2026, 6, 2, 15, 0, tzinfo=timezone.utc))
    assert not is_market_open(us, datetime(2026, 6, 6, 15, 0, tzinfo=timezone.utc))  # Saturday
    # 06:00 UTC = 13:00 Bangkok -> lunch break
    assert not is_market_open(th, datetime(2026, 6, 2, 6, 0))
    assert is_market_open(th, datetime(2026, 6, 2, 4, 0))  # 11:00 Bangkok
    assert not is_market_open(th, datetime(2026, 6, 3, 4, 0))  # holiday


def test_retry_eventually_succeeds_and_gives_up():
    calls = {"n": 0}

    @retry(exceptions=ValueError, tries=3, delay=0, sleep=lambda _: None)
    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ValueError("fail")
        return "ok"

    assert flaky() == "ok" and calls["n"] == 3

    @retry(exceptions=ValueError, tries=2, delay=0, sleep=lambda _: None)
    def always():
        raise ValueError("nope")

    with pytest.raises(ValueError):
        always()


class Counter(Notifier):
    name = "counter"

    def __init__(self):
        self.n = 0

    def send(self, title, message, level="INFO"):
        self.n += 1
        return True


def test_alert_cooldown_and_thresholds(settings):
    settings.alerts.cooldown_minutes = 60
    c = Counter()
    am = AlertManager(settings.alerts, c)
    ts = datetime(2026, 1, 2)
    assert not am.price_move("AAA", 1.0, 10, ts)  # below 5% threshold
    assert am.price_move("AAA", -6.0, 10, ts)
    assert not am.price_move("AAA", -6.0, 10, ts)  # same bar -> cooldown
    assert am.drawdown("pf", 0.12, 0.15)
    assert not am.drawdown("pf", 0.13, 0.15)  # cooldown
    am.signal("AAA", "HOLD", 1, "", "x")  # HOLD never alerts
    assert c.n == 2


def test_http_notifiers_never_raise():
    class BoomSession(requests.Session):
        def post(self, *a, **k):
            raise requests.ConnectionError("offline")

    assert TelegramNotifier("t", "c", session=BoomSession()).send("t", "m") is False
    assert DiscordNotifier("http://x", session=BoomSession()).send("t", "m") is False

    class Crash(Notifier):
        name = "crash"

        def send(self, *a, **k):
            raise RuntimeError("bug")

    assert MultiNotifier([Crash(), Counter()]).send("t", "m") is True


def test_build_notifier_skips_unconfigured(settings):
    settings.alerts.channels = ["telegram", "discord"]
    n = build_notifier(settings.alerts)
    assert [x.name for x in n.notifiers] == ["console"]


def test_dashboard_endpoints(settings, db):
    from dashboard.app import create_app
    from data.providers import SyntheticProvider
    from tests.test_engine import build_engine

    engine, _ = build_engine(settings, db, SyntheticProvider(end=datetime(2026, 6, 30)))
    engine.run_cycle()
    client = TestClient(create_app(settings, db))
    assert client.get("/").status_code == 200
    pf = client.get("/api/portfolio").json()
    assert pf["equity"] == pytest.approx(settings.execution.initial_capital)
    for path in ["/api/positions", "/api/trades", "/api/orders", "/api/signals?include_hold=true", "/api/equity",
                 "/api/pnl/daily", "/api/pnl/monthly", "/api/daily-summary", "/api/prices/AAA", "/api/alerts"]:
        assert client.get(path).status_code == 200, path
    assert len(client.get("/api/signals?include_hold=true").json()) == 2
    assert client.get("/api/pnl/weekly").status_code == 400


def test_demo_command_offline(monkeypatch, capsys):
    """`python main.py demo --offline` runs every step end to end (paper, synthetic data)."""
    import main
    from config.settings import PROJECT_ROOT

    monkeypatch.setattr(main, "_demo_name", lambda config, mode: f"demo_pytest_{mode}")
    db_file = PROJECT_ROOT / "data" / "demo_pytest_offline.db"
    try:
        assert main.main(["demo", "--offline"]) == 0
        out = capsys.readouterr().out
        assert "MODE: OFFLINE" in out and "[5/5] Portfolio" in out and "Strategy comparison" in out
        assert db_file.exists()
    finally:
        for f in db_file.parent.glob("demo_pytest_offline.db*"):
            f.unlink()
