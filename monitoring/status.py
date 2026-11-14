"""Live bot status for the /monitor page, written to ``data/status.json``.

The engine/scheduler call :class:`StatusWriter` on every step; the dashboard only reads the file.
Writes are atomic (temp file + rename) so the page never sees a half-written file.
All timestamps are timezone-aware ISO-8601 in UTC; the UI converts to Thai time.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from loguru import logger

from config.settings import PROJECT_ROOT, Settings

STEPS: list[tuple[str, str]] = [
    ("fetch", "ดึงราคา"),
    ("signal", "คำนวณสัญญาณ"),
    ("risk", "ตรวจความเสี่ยง"),
    ("order", "ส่งคำสั่งจำลอง"),
    ("wait", "รอรอบถัดไป"),
]
STEP_INDEX = {key: i + 1 for i, (key, _) in enumerate(STEPS)}
STEP_LABEL = dict(STEPS)
LOG_LINES = 20
GUARD_HISTORY = 20
STALE_AFTER_SECONDS = 300  # "running" without any update for this long = crashed/stuck

# TradingView symbol mapping (free widget). Everything not listed falls back to the rule below.
TV_EXCHANGE = {"AAPL": "NASDAQ", "MSFT": "NASDAQ", "NVDA": "NASDAQ", "SPY": "AMEX"}


def tv_symbol(symbol: str) -> str:
    """AAPL -> NASDAQ:AAPL, SPY -> AMEX:SPY, PTT.BK -> SET:PTT."""
    s = symbol.upper()
    if s.endswith(".BK"):
        return f"SET:{s[:-3]}"
    return f"{TV_EXCHANGE[s]}:{s}" if s in TV_EXCHANGE else s


def status_path(settings: Settings) -> Path:
    p = Path(settings.dashboard.status_file)
    return p if p.is_absolute() else PROJECT_ROOT / p


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:  # engine clock is naive-UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def read_status(path: str | Path) -> dict[str, Any] | None:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


class StatusWriter:
    """Collects bot state in memory and mirrors it to ``path`` (``None`` = memory only)."""

    def __init__(self, path: str | Path | None, *, timezone_name: str = "Asia/Bangkok", mode: str = "paper",
                 guard_config: dict[str, Any] | None = None) -> None:
        self.path = Path(path) if path else None
        self._lock = threading.RLock()
        self._sink_id: int | None = None
        self._order: list[str] = []
        prev = read_status(self.path) if self.path else None
        prev = prev or {}
        self._logs: deque[dict[str, str]] = deque(prev.get("logs", [])[-LOG_LINES:], maxlen=LOG_LINES)
        self._guard_recent: deque[dict[str, Any]] = deque(
            prev.get("guard", {}).get("recent", [])[-GUARD_HISTORY:], maxlen=GUARD_HISTORY)
        self._tz = timezone_name
        self._today = self._local_date()
        g = prev.get("guard", {}).get("today", {})
        self._guard_today = {"passed": 0, "blocked": 0}
        if g.get("date") == self._today:
            self._guard_today = {"passed": int(g.get("passed", 0)), "blocked": int(g.get("blocked", 0))}
        self.state: dict[str, Any] = {
            "version": 1,
            "mode": "PAPER",  # the bot is paper-only; the UI shows a PAPER badge on every number
            "paper": mode != "live",
            "timezone": timezone_name,
            "state": "idle",
            "round": {"id": int(prev.get("round", {}).get("id", 0)), "started_at": None, "finished_at": None,
                      "interval": None, "trigger": None},
            "step": {"index": 5, "key": "wait", "label": STEP_LABEL["wait"], "total": len(STEPS),
                     "symbol": None, "symbol_index": 0, "symbol_total": 0, "detail": "ยังไม่เริ่มรอบแรก"},
            "last_run_at": prev.get("last_run_at"),
            "next_run_at": prev.get("next_run_at"),
            "every_minutes": prev.get("every_minutes"),
            "watchlist": prev.get("watchlist", []),
            "portfolio": prev.get("portfolio"),
            "guard": {"config": guard_config or {}, "last": prev.get("guard", {}).get("last"), "today": {}, "recent": []},
            "logs": [],
        }
        self.flush()

    # ------------------------------------------------------------------ internals
    def _local_date(self) -> str:
        return datetime.now(ZoneInfo(self._tz)).date().isoformat()

    def _roll_day(self) -> None:
        today = self._local_date()
        if today != self._today:
            self._today, self._guard_today = today, {"passed": 0, "blocked": 0}

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            self._roll_day()
            st = dict(self.state)
            st["updated_at"] = _iso(_now())
            st["logs"] = list(self._logs)
            guard = dict(st["guard"])
            guard["today"] = {"date": self._today, **self._guard_today}
            guard["recent"] = list(self._guard_recent)
            st["guard"] = guard
            return st

    def _adopt_newer_round(self) -> None:
        """The scheduler and the dashboard ("run now") each own a StatusWriter for the same file.
        If the other process already finished a newer round, adopt its round data instead of
        overwriting the file with our older snapshot (and so the next round id keeps counting up)."""
        if self.path is None or self.state.get("state") == "running":
            return
        other = read_status(self.path) or {}
        theirs = int((other.get("round") or {}).get("id") or 0)
        ours = int((self.state.get("round") or {}).get("id") or 0)
        if theirs <= ours or other.get("state") == "running":
            return
        for key in ("state", "round", "step", "last_run_at", "watchlist", "portfolio"):
            if key in other:
                self.state[key] = other[key]
        merged = {(l.get("ts"), l.get("text")): l for l in list(self._logs) + list(other.get("logs", []))}
        self._logs.clear()
        self._logs.extend(sorted(merged.values(), key=lambda l: l.get("ts") or "")[-LOG_LINES:])

    def flush(self) -> None:
        if self.path is None:
            return
        with self._lock:
            self._adopt_newer_round()
            data = json.dumps(self.snapshot(), ensure_ascii=False, indent=1, default=str)
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".status-", suffix=".tmp")
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(data)
                os.replace(tmp, self.path)
            except OSError:  # status is best-effort; never break trading because of it
                pass

    # ------------------------------------------------------------------ rounds / steps
    @property
    def running(self) -> bool:
        return self.state["state"] == "running"

    def begin_round(self, interval: str, started_at: datetime, symbol_total: int, trigger: str = "schedule") -> int:
        with self._lock:
            self._adopt_newer_round()
            rnd = self.state["round"]
            rnd.update(id=rnd["id"] + 1, started_at=_iso(started_at), finished_at=None, interval=interval,
                       trigger=trigger)
            self.state["state"] = "running"
            self.state["step"]["symbol_total"] = symbol_total
            self.step("fetch", symbol=None, symbol_index=0, detail="เริ่มรอบ")
            return rnd["id"]

    def step(self, key: str, *, symbol: str | None = None, symbol_index: int | None = None,
             detail: str = "") -> None:
        with self._lock:
            s = self.state["step"]
            s.update(index=STEP_INDEX[key], key=key, label=STEP_LABEL[key], symbol=symbol, detail=detail)
            if symbol_index is not None:
                s["symbol_index"] = symbol_index
            self.flush()

    def end_round(self, finished_at: datetime) -> None:
        with self._lock:
            self.state["round"]["finished_at"] = _iso(finished_at)
            self.state["last_run_at"] = _iso(finished_at)
            self.state["state"] = "idle"
            self.step("wait", symbol=None, detail="รอรอบถัดไป")

    def set_next_run(self, next_run: datetime | None, every_minutes: float | None = None) -> None:
        with self._lock:
            self.state["next_run_at"] = _iso(next_run)
            if every_minutes is not None:
                self.state["every_minutes"] = every_minutes
            self.flush()

    # ------------------------------------------------------------------ data
    def update_symbol(self, entry: dict[str, Any]) -> None:
        with self._lock:
            wl = [w for w in self.state["watchlist"] if w.get("symbol") != entry["symbol"]]
            wl.append({"tv_symbol": tv_symbol(entry["symbol"]), **entry})
            order = {s: i for i, s in enumerate(self._order)}
            self.state["watchlist"] = sorted(wl, key=lambda w: order.get(w["symbol"], 999))
            self.flush()

    def set_watchlist_order(self, symbols: list[str]) -> None:
        with self._lock:
            self._order = list(symbols)
            known = {w["symbol"] for w in self.state["watchlist"]}
            # drop symbols that left the watchlist, add placeholders for new ones
            self.state["watchlist"] = [w for w in self.state["watchlist"] if w["symbol"] in symbols]
            for s in symbols:
                if s not in known:
                    self.state["watchlist"].append({"symbol": s, "tv_symbol": tv_symbol(s), "signal": None,
                                                    "price": None, "percent_change": None, "strategy": None,
                                                    "action": "", "blocked": False, "error": None})

    def set_portfolio(self, pf: dict[str, Any]) -> None:
        with self._lock:
            self.state["portfolio"] = pf
            self.flush()

    def record_guard(self, symbol: str, side: str, quantity: float, price: float, decision: dict[str, Any],
                     *, round_id: int, at: datetime, discord_sent: bool | None = None) -> None:
        """Remember the full 4-check result of the latest order (passed or blocked)."""
        with self._lock:
            self._roll_day()
            entry = {
                "round_id": round_id, "at": _iso(at), "symbol": symbol, "side": side, "quantity": quantity,
                "price": price, "paper": True, "allowed": decision["allowed"], "reason": decision["reason"],
                "checks": decision["checks"], "alerted": discord_sent,
            }
            self.state["guard"]["last"] = entry
            self._guard_today["passed" if decision["allowed"] else "blocked"] += 1
            if not decision["allowed"]:
                self._guard_recent.append(entry)
            self.flush()

    # ------------------------------------------------------------------ log
    def log(self, level: str, text: str, at: datetime | None = None) -> None:
        with self._lock:
            self._logs.append({"ts": _iso(at or _now()) or "", "level": level, "text": text})
            self.flush()

    def attach_logger(self) -> None:
        """Mirror loguru INFO+ lines into the status log (keeps the last 20)."""
        if self._sink_id is not None:
            return

        def sink(message: Any) -> None:
            rec = message.record
            level = {"WARNING": "WARN"}.get(rec["level"].name, rec["level"].name)
            try:
                self.log(level, str(rec["message"]).splitlines()[0][:300], rec["time"])
            except Exception:  # never let the status mirror break logging
                pass

        self._sink_id = logger.add(sink, level="INFO", format="{message}")

    def detach_logger(self) -> None:
        if self._sink_id is not None:
            logger.remove(self._sink_id)
            self._sink_id = None


def build_status_writer(settings: Settings) -> StatusWriter:
    """StatusWriter for the configured ``dashboard.status_file`` (shared by scheduler and dashboard)."""
    return StatusWriter(
        status_path(settings), timezone_name="Asia/Bangkok", mode=settings.execution.mode,
        guard_config=guard_config(settings),
    )


def guard_config(settings: Settings) -> dict[str, Any]:
    g = settings.price_guard
    return {"enabled": g.enabled, "max_deviation_pct": g.max_deviation_pct,
            "max_daily_jump_pct": g.max_daily_jump_pct, "max_order_value": g.max_order_value,
            "max_bar_age_days": g.max_bar_age_days, "max_bar_age_minutes": g.max_bar_age_minutes}
