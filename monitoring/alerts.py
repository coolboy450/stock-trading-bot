"""Alert rules: signals, trades, big price moves, drawdown, errors, daily report."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta

from loguru import logger

from config.settings import AlertSettings
from database.models import AlertLog, utcnow
from database.session import Database
from monitoring.notifier import MultiNotifier, Notifier


class AlertManager:
    """Sends alerts through a notifier with per-key cooldown and DB logging."""

    def __init__(self, settings: AlertSettings, notifier: Notifier, db: Database | None = None) -> None:
        self.settings = settings
        self.notifier = notifier
        self.db = db
        self._last_sent: dict[str, datetime] = {}
        self._buffer: list[AlertLog] | None = None

    @contextmanager
    def deferred(self) -> Iterator[None]:
        """Buffer DB alert logs until the block exits.

        Wrap the caller's own DB transaction with this so the alert log is written
        *after* it commits (SQLite allows only one writer at a time).
        """
        if self._buffer is not None:  # already deferring (nested)
            yield
            return
        self._buffer = []
        try:
            yield
        finally:
            pending, self._buffer = self._buffer, None
            self._write(pending)

    # ------------------------------------------------------------------ core
    def send(self, alert_type: str, title: str, message: str, level: str = "INFO", *, key: str | None = None) -> bool:
        """Send unless the same ``key`` fired within ``cooldown_minutes``."""
        now = utcnow()
        if key is not None:
            last = self._last_sent.get(key)
            if last and now - last < timedelta(minutes=self.settings.cooldown_minutes):
                logger.debug("Alert {} suppressed by cooldown", key)
                return False
            self._last_sent[key] = now
        error = None
        try:
            delivered = self.notifier.send(title, message, level)
        except Exception as exc:  # pragma: no cover - notifiers already swallow errors
            delivered, error = False, str(exc)
        self._log(alert_type, level, title, message, delivered, error)
        return delivered

    def _log(self, alert_type: str, level: str, title: str, message: str, delivered: bool, error: str | None) -> None:
        if self.db is None:
            return
        channels = self.notifier.channel_names if isinstance(self.notifier, MultiNotifier) else self.notifier.name
        entry = AlertLog(alert_type=alert_type, level=level, title=title, message=message,
                         channels=channels, delivered=delivered, error=error, created_at=utcnow())
        if self._buffer is not None:
            self._buffer.append(entry)
        else:
            self._write([entry])

    def _write(self, entries: list[AlertLog]) -> None:
        if not entries or self.db is None:
            return
        try:
            with self.db.session() as s:
                s.add_all(entries)
        except Exception as exc:
            logger.error("Could not log {} alert(s) to DB: {}", len(entries), exc)

    # ------------------------------------------------------------------ rules
    def signal(self, symbol: str, signal_type: str, price: float, reason: str, strategy: str) -> None:
        if self.settings.notify_signals and signal_type != "HOLD":
            self.send(
                "SIGNAL",
                f"{signal_type} signal: {symbol}",
                f"Price: {price:,.4f}\nStrategy: {strategy}\nReason: {reason}",
                "INFO",
            )

    def trade(self, action: str, symbol: str, qty: float, price: float, *, pnl: float | None = None,
              mode: str = "PAPER", reason: str = "") -> None:
        if not self.settings.notify_trades:
            return
        msg = f"[{mode}] {action} {qty:g} {symbol} @ {price:,.4f}"
        if pnl is not None:
            msg += f"\nPnL: {pnl:+,.2f}"
        if reason:
            msg += f"\nReason: {reason}"
        self.send("TRADE", f"{action} {symbol}", msg, "SUCCESS" if (pnl or 0) >= 0 else "WARNING")

    def price_move(self, symbol: str, percent_change: float | None, close: float, bar_time: datetime) -> bool:
        if percent_change is None or abs(percent_change) < self.settings.price_move_pct:
            return False
        direction = "UP 📈" if percent_change > 0 else "DOWN 📉"
        return self.send(
            "PRICE_MOVE",
            f"{symbol} {direction} {percent_change:+.2f}%",
            f"Close {close:,.4f} at {bar_time:%Y-%m-%d %H:%M} UTC (threshold ±{self.settings.price_move_pct}%)",
            "WARNING",
            key=f"move:{symbol}:{bar_time.isoformat()}",
        )

    def drawdown(self, portfolio: str, drawdown_pct: float, limit_pct: float) -> bool:
        if drawdown_pct < self.settings.drawdown_alert_pct:
            return False
        blocked = " - new entries BLOCKED" if drawdown_pct >= limit_pct else ""
        return self.send(
            "DRAWDOWN",
            f"Drawdown {drawdown_pct:.2%} on {portfolio}",
            f"Alert threshold {self.settings.drawdown_alert_pct:.0%}, hard limit {limit_pct:.0%}{blocked}",
            "ERROR",
            key=f"dd:{portfolio}",
        )

    def risk_block(self, symbol: str, reason: str) -> None:
        self.send("RISK", f"Entry blocked: {symbol}", reason, "WARNING", key=f"risk:{reason[:40]}")

    def error(self, where: str, exc: BaseException | str) -> None:
        self.send("ERROR", f"Error in {where}", str(exc)[:1500], "ERROR", key=f"err:{where}:{str(exc)[:80]}")

    def report(self, title: str, body: str) -> None:
        self.send("REPORT", title, body, "INFO")
