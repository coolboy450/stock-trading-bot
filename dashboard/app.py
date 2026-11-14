"""FastAPI dashboard: JSON API + a single-page HTML view (``/``).

Run: ``python main.py dashboard`` then open http://127.0.0.1:8000
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from config.settings import Settings
from database import repository as repo
from database.models import AlertLog, DailySummary, Order, Portfolio, Signal, SignalType, Stock, Trade, TradeStatus
from database.session import Database
from monitoring.status import STALE_AFTER_SECONDS, STEPS, guard_config, read_status, status_path, tv_symbol

STATIC = Path(__file__).parent / "static"


def _paper_cycle_runner(settings: Settings, db: Database) -> Callable[[], None]:
    """Default "run now" action: one paper cycle with a PaperBroker. Never builds a live broker."""

    def run() -> None:
        from data.providers import build_provider
        from execution.broker import PaperBroker
        from execution.costs import CostModel
        from execution.engine import TradingEngine
        from monitoring.alerts import AlertManager
        from monitoring.notifier import build_notifier
        from monitoring.status import build_status_writer

        status = build_status_writer(settings)
        status.attach_logger()
        try:
            engine = TradingEngine(
                settings, db, build_provider(settings.data), PaperBroker(CostModel.from_settings(settings.execution)),
                AlertManager(settings.alerts, build_notifier(settings.alerts), db), status=status,
            )
            engine.run_cycle(trigger="manual")
        finally:
            status.detach_logger()

    return run


def create_app(
    settings: Settings, db: Database | None = None, run_cycle: Callable[[], None] | None = None,
    status_file: Path | None = None,
) -> FastAPI:
    """``run_cycle`` / ``status_file`` can be injected (tests); defaults are the paper runner and config path."""
    db = db or Database(settings.database.url)
    status_file = status_file or status_path(settings)
    run_cycle = run_cycle or _paper_cycle_runner(settings, db)
    run_lock = threading.Lock()
    app = FastAPI(title="Stock Bot Dashboard", version="1.0")
    pf_name = settings.execution.portfolio_name
    app.mount("/static", StaticFiles(directory=STATIC), name="static")  # favicon + step icons (read-only)

    def _portfolio(s) -> Portfolio:
        pf = s.scalar(select(Portfolio).where(Portfolio.name == pf_name))
        if pf is None:
            raise HTTPException(404, f"Portfolio '{pf_name}' not found - run a trading cycle first")
        return pf

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (STATIC / "index.html").read_text(encoding="utf-8")

    # ------------------------------------------------------------------ /monitor
    def _monitor_state() -> dict:
        """status.json + derived flags. Never raises: a missing file means "bot has not run yet"."""
        data = read_status(status_file)
        found = data is not None
        if data is None:
            data = {
                "version": 1, "mode": "PAPER", "state": "idle", "round": {"id": 0}, "watchlist": [],
                "step": {"index": 5, "key": "wait", "label": dict(STEPS)["wait"], "total": len(STEPS),
                         "symbol": None, "symbol_index": 0, "symbol_total": 0, "detail": "ยังไม่มีข้อมูลจากบอท"},
                "last_run_at": None, "next_run_at": None, "portfolio": None, "logs": [],
                "guard": {"last": None, "today": {"passed": 0, "blocked": 0}, "recent": []},
            }
        now = datetime.now(timezone.utc)
        try:
            age = (now - datetime.fromisoformat(data["updated_at"])).total_seconds()
        except (KeyError, TypeError, ValueError):
            age = None
        running = data.get("state") == "running"
        stale = running and (age is None or age > STALE_AFTER_SECONDS)  # crashed mid-round
        data["paper"] = True
        data["mode"] = "PAPER"
        data["status_file_found"] = found
        data["server_time"] = now.isoformat(timespec="seconds")
        data["heartbeat_age_seconds"] = round(age, 1) if age is not None else None
        data["online"] = age is not None and age < 90
        data["stale"] = stale
        data["running"] = running and not stale
        data["can_run"] = not settings.is_live and not (running and not stale) and not run_lock.locked()
        data.setdefault("guard", {})["config"] = guard_config(settings)  # always the live config values
        data["is_live_config"] = settings.is_live  # True = run-now refuses
        for w in data.get("watchlist", []):
            w.setdefault("tv_symbol", tv_symbol(w["symbol"]))
        return data

    @app.get("/monitor", response_class=HTMLResponse)
    def monitor_page() -> str:
        page = STATIC / "monitor.html"
        if not page.exists():
            raise HTTPException(404, "monitor.html not built yet")
        return page.read_text(encoding="utf-8")

    @app.get("/api/monitor/status")
    def monitor_status() -> JSONResponse:
        return JSONResponse(_monitor_state(), headers={"Cache-Control": "no-store"})

    @app.post("/api/monitor/run-now", status_code=202)
    def monitor_run_now() -> dict:
        """Run one PAPER cycle in the background. Refused while a round is running or in live config."""
        if settings.is_live:
            raise HTTPException(403, "run-now is paper-only; config is in live mode")
        if not run_lock.acquire(blocking=False):
            raise HTTPException(409, "a round is already running")
        current = read_status(status_file) or {}
        if current.get("state") == "running" and not _monitor_state()["stale"]:  # e.g. the scheduler process
            run_lock.release()
            raise HTTPException(409, "a round is already running")

        def work() -> None:
            try:
                run_cycle()
            finally:
                run_lock.release()

        try:
            threading.Thread(target=work, name="monitor-run-now", daemon=True).start()
        except Exception:
            run_lock.release()
            raise
        return {"accepted": True, "paper": True, "next_round_id": int(current.get("round", {}).get("id", 0)) + 1}

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok", "mode": settings.execution.mode, "portfolio": pf_name}

    @app.get("/api/portfolio")
    def portfolio() -> dict:
        with db.session() as s:
            pf = _portfolio(s)
            closed = list(s.scalars(select(Trade).where(Trade.portfolio_id == pf.id, Trade.status == TradeStatus.CLOSED)))
            wins = [t for t in closed if (t.pnl or 0) > 0]
            return {
                "name": pf.name, "mode": pf.mode.value, "currency": pf.currency,
                "initial_capital": pf.initial_capital, "cash": pf.cash, "positions_value": pf.positions_value,
                "equity": pf.equity, "peak_equity": pf.peak_equity, "drawdown_pct": pf.drawdown_pct * 100,
                "realized_pnl": pf.realized_pnl,
                "total_return_pct": (pf.equity / pf.initial_capital - 1) * 100,
                "closed_trades": len(closed),
                "win_rate_pct": len(wins) / len(closed) * 100 if closed else None,
                "updated_at": pf.updated_at.isoformat(),
            }

    @app.get("/api/positions")
    def positions() -> list[dict]:
        with db.session() as s:
            pf = _portfolio(s)
            out = []
            for t in repo.open_trades(s, pf.id):
                last = repo.latest_close(s, t.stock_id) or t.entry_price
                out.append({
                    "symbol": t.stock.symbol, "quantity": t.quantity, "entry_price": t.entry_price,
                    "last_price": last, "market_value": last * t.quantity,
                    "unrealized_pnl": (last - t.entry_price) * t.quantity - t.entry_commission,
                    "unrealized_pct": (last / t.entry_price - 1) * 100,
                    "stop_loss": t.stop_loss, "take_profit": t.take_profit,
                    "entry_time": t.entry_time.isoformat(), "strategy": t.strategy_used,
                })
            return out

    @app.get("/api/trades")
    def trades(limit: int = Query(50, le=500)) -> list[dict]:
        with db.session() as s:
            pf = _portfolio(s)
            rows = s.scalars(select(Trade).where(Trade.portfolio_id == pf.id).order_by(Trade.id.desc()).limit(limit))
            return [{
                "id": t.id, "symbol": t.stock.symbol, "status": t.status.value, "strategy": t.strategy_used,
                "quantity": t.quantity, "entry_price": t.entry_price, "entry_time": t.entry_time.isoformat(),
                "exit_price": t.exit_price, "exit_time": t.exit_time.isoformat() if t.exit_time else None,
                "pnl": t.pnl, "pnl_pct": t.pnl_pct, "exit_reason": t.exit_reason.value if t.exit_reason else None,
            } for t in rows]

    @app.get("/api/orders")
    def orders(limit: int = Query(50, le=500)) -> list[dict]:
        with db.session() as s:
            pf = _portfolio(s)
            rows = s.scalars(select(Order).where(Order.portfolio_id == pf.id).order_by(Order.id.desc()).limit(limit))
            return [{
                "id": o.id, "symbol": o.stock.symbol, "side": o.side.value, "type": o.order_type.value,
                "quantity": o.quantity, "status": o.status.value, "filled_price": o.filled_price,
                "commission": o.commission, "slippage": o.slippage, "purpose": o.purpose,
                "created_at": o.created_at.isoformat(), "message": o.message,
            } for o in rows]

    @app.get("/api/signals")
    def signals(limit: int = Query(50, le=500), include_hold: bool = False) -> list[dict]:
        with db.session() as s:
            stmt = select(Signal).order_by(Signal.timestamp.desc(), Signal.id.desc()).limit(limit)
            if not include_hold:
                stmt = stmt.where(Signal.signal_type != SignalType.HOLD)
            return [{
                "symbol": g.stock.symbol, "type": g.signal_type.value, "strategy": g.strategy, "price": g.price,
                "timestamp": g.timestamp.isoformat(), "reason": g.reason, "acted_on": g.acted_on,
                "note": g.action_note,
            } for g in s.scalars(stmt)]

    @app.get("/api/equity")
    def equity() -> list[dict]:
        with db.session() as s:
            df = repo.snapshots_df(s, _portfolio(s).id)
            return [{"timestamp": ts.isoformat(), "equity": r.equity, "drawdown_pct": r.drawdown_pct * 100}
                    for ts, r in df.iterrows()]

    @app.get("/api/pnl/{freq}")
    def pnl(freq: str) -> list[dict]:
        if freq not in {"daily", "monthly"}:
            raise HTTPException(400, "freq must be daily or monthly")
        with db.session() as s:
            return repo.period_pnl(s, _portfolio(s), "M" if freq == "monthly" else "D").to_dict(orient="records")

    @app.get("/api/daily-summary")
    def daily_summary(limit: int = Query(50, le=500)) -> list[dict]:
        with db.session() as s:
            rows = s.scalars(select(DailySummary).order_by(DailySummary.date.desc(),
                                                           DailySummary.percent_change.desc()).limit(limit))
            return [{
                "date": r.date.isoformat(), "symbol": r.stock.symbol, "close": r.close,
                "percent_change": r.percent_change, "direction": r.direction.value if r.direction else None,
                "volume": r.volume, "last_signal": r.last_signal,
            } for r in rows]

    @app.get("/api/prices/{symbol}")
    def prices(symbol: str, interval: str = "1d", limit: int = Query(200, le=2000)) -> list[dict]:
        with db.session() as s:
            stock = s.scalar(select(Stock).where(Stock.symbol == symbol.upper()))
            if stock is None:
                raise HTTPException(404, "unknown symbol")
            df = repo.get_price_df(s, stock.id, interval, limit=limit)
            return [{"timestamp": ts.isoformat(), "close": r.close, "percent_change": r.percent_change,
                     "direction": r.direction} for ts, r in df.iterrows()]

    @app.get("/api/alerts")
    def alerts(limit: int = Query(30, le=200)) -> list[dict]:
        with db.session() as s:
            rows = s.scalars(select(AlertLog).order_by(AlertLog.id.desc()).limit(limit))
            return [{"type": a.alert_type, "level": a.level, "title": a.title, "message": a.message,
                     "delivered": a.delivered, "created_at": a.created_at.isoformat()} for a in rows]

    return app
