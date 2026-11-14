"""/monitor endpoints, status.json writer, TradingView symbol mapping (paper only)."""

import json
import threading
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from dashboard.app import create_app
from monitoring.status import StatusWriter, read_status, tv_symbol


@pytest.mark.parametrize("sym,expected", [
    ("AAPL", "NASDAQ:AAPL"), ("MSFT", "NASDAQ:MSFT"), ("NVDA", "NASDAQ:NVDA"), ("SPY", "AMEX:SPY"),
    ("PTT.BK", "SET:PTT"), ("ptt.bk", "SET:PTT"),
])
def test_tv_symbol(sym, expected):
    assert tv_symbol(sym) == expected


def make_client(settings, db, tmp_path, run_cycle=None):
    path = tmp_path / "status.json"
    app = create_app(settings, db, run_cycle=run_cycle or (lambda: None), status_file=path)
    return TestClient(app), path


def test_status_without_file(settings, db, tmp_path):
    client, _ = make_client(settings, db, tmp_path)
    r = client.get("/api/monitor/status")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    j = r.json()
    assert j["paper"] is True and j["mode"] == "PAPER"
    assert j["status_file_found"] is False and j["online"] is False and j["can_run"] is True
    assert j["guard"]["config"]["max_deviation_pct"] == 0.02


def test_status_reads_file_written_by_writer(settings, db, tmp_path):
    client, path = make_client(settings, db, tmp_path)
    w = StatusWriter(path)
    w.set_watchlist_order(["AAPL", "SPY", "PTT.BK"])
    w.begin_round("1d", datetime(2026, 11, 13, 10, 0), 3)
    w.step("signal", symbol="AAPL", detail="x")
    w.update_symbol({"symbol": "AAPL", "signal": "BUY", "price": 200.0, "percent_change": 1.2,
                     "strategy": "ma_crossover", "action": "", "blocked": False, "error": None})
    j = client.get("/api/monitor/status").json()
    assert j["state"] == "running" and j["running"] is True and j["can_run"] is False
    assert j["step"]["key"] == "signal" and j["step"]["index"] == 2
    assert [x["tv_symbol"] for x in j["watchlist"]] == ["NASDAQ:AAPL", "AMEX:SPY", "SET:PTT"]
    assert j["online"] is True and j["status_file_found"] is True


def test_stale_running_state_is_not_blocking(settings, db, tmp_path):
    client, path = make_client(settings, db, tmp_path)
    StatusWriter(path).begin_round("1d", datetime(2026, 11, 13), 1)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["updated_at"] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    path.write_text(json.dumps(data), encoding="utf-8")
    j = client.get("/api/monitor/status").json()
    assert j["stale"] is True and j["running"] is False and j["can_run"] is True and j["online"] is False


def test_corrupt_status_file_does_not_crash(settings, db, tmp_path):
    client, path = make_client(settings, db, tmp_path)
    path.write_text("{not json", encoding="utf-8")
    assert client.get("/api/monitor/status").status_code == 200


def test_logs_keep_last_20_and_guard_blocks_exposed(tmp_path):
    path = tmp_path / "s.json"
    w = StatusWriter(path)
    for i in range(30):
        w.log("INFO", f"line {i}")
    decision = {"allowed": False, "reason": "x", "checks": []}
    w.record_guard("AAPL", "BUY", 1, 100.0, decision, round_id=1, at=datetime(2026, 11, 13))
    data = read_status(path)
    assert len(data["logs"]) == 20 and data["logs"][-1]["text"] == "line 29"
    assert data["guard"]["today"]["blocked"] == 1 and data["guard"]["recent"][0]["paper"] is True
    # a new writer (process restart) keeps history
    assert len(StatusWriter(path).snapshot()["logs"]) == 20


def test_run_now_runs_once_and_blocks_double_click(settings, db, tmp_path):
    started, release, calls = threading.Event(), threading.Event(), []

    def slow_cycle():
        calls.append(1)
        started.set()
        release.wait(5)

    client, _ = make_client(settings, db, tmp_path, run_cycle=slow_cycle)
    first = client.post("/api/monitor/run-now")
    assert first.status_code == 202 and first.json()["paper"] is True
    assert started.wait(5)
    assert client.post("/api/monitor/run-now").status_code == 409
    assert client.get("/api/monitor/status").json()["can_run"] is False
    release.set()
    for _ in range(100):  # lock is released when the worker thread finishes
        if client.get("/api/monitor/status").json()["can_run"]:
            break
        threading.Event().wait(0.05)
    assert client.post("/api/monitor/run-now").status_code == 202
    assert len(calls) >= 1


def test_run_now_refused_while_scheduler_round_running(settings, db, tmp_path):
    client, path = make_client(settings, db, tmp_path)
    StatusWriter(path).begin_round("1d", datetime(2026, 11, 13), 1)
    assert client.post("/api/monitor/run-now").status_code == 409


def test_run_now_refused_in_live_config(settings, db, tmp_path):
    called = []
    client, _ = make_client(settings, db, tmp_path, run_cycle=lambda: called.append(1))
    # is_live is derived from config; patch the property on the class for this check only
    from config.settings import Settings

    original = Settings.is_live
    Settings.is_live = property(lambda self: True)
    try:
        assert client.post("/api/monitor/run-now").status_code == 403
        j = client.get("/api/monitor/status").json()
        assert j["can_run"] is False and j["is_live_config"] is True
    finally:
        Settings.is_live = original
    assert called == []


def test_monitor_page_served(settings, db, tmp_path):
    client, _ = make_client(settings, db, tmp_path)
    r = client.get("/monitor")
    assert r.status_code == 200
    assert 'href="/static/favicon.svg"' in r.text
    assert "var ICONS" in r.text  # inline step icons
    for k in ("fetch", "signal", "risk", "order", "wait"):
        assert f"{k}: SVG_OPEN" in r.text


def test_static_assets_served(settings, db, tmp_path):
    client, _ = make_client(settings, db, tmp_path)
    r = client.get("/static/favicon.svg")
    assert r.status_code == 200 and "svg" in r.headers["content-type"]
    for k in ("fetch", "signal", "risk", "order", "wait"):
        assert client.get("/static/icon-step-" + k + ".svg").status_code == 200
    assert client.get("/static/nope.svg").status_code == 404


def test_attach_logger_mirrors_loguru_lines(tmp_path):
    """Regression: the loguru sink must read record["level"] (record is a dict)."""
    from loguru import logger
    from monitoring.status import StatusWriter

    w = StatusWriter(tmp_path / "status.json", timezone_name="Asia/Bangkok", mode="paper", guard_config={})
    w.attach_logger()
    try:
        logger.warning("guard test line")
    finally:
        w.detach_logger()
    logs = w._logs
    assert logs and logs[-1]["text"] == "guard test line" and logs[-1]["level"] == "WARN"


def test_two_writers_do_not_clobber_newer_round(tmp_path):
    """Regression: the scheduler heartbeat must not overwrite a round finished by run-now in the dashboard."""
    from datetime import datetime, timezone
    from monitoring.status import StatusWriter, read_status

    path = tmp_path / "status.json"
    scheduler = StatusWriter(path, timezone_name="Asia/Bangkok", mode="paper", guard_config={})
    scheduler.flush()                      # scheduler starts first: round 0
    dashboard = StatusWriter(path, timezone_name="Asia/Bangkok", mode="paper", guard_config={})
    now = datetime.now(timezone.utc)
    dashboard.begin_round("1d", now, symbol_total=4, trigger="manual")
    dashboard.end_round(now)               # dashboard finishes round 1
    scheduler.flush()                      # heartbeat from the stale scheduler writer
    on_disk = read_status(path)
    assert on_disk["round"]["id"] == 1 and on_disk["last_run_at"] is not None
    assert scheduler.begin_round("1d", now, symbol_total=4) == 2   # next scheduled round keeps counting
