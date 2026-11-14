"""JSON storage for the weekly strategy review (overrides, pending changes, history, last result).

* ``data/strategy_overrides.json`` - per-symbol strategy chosen by a review, pending changes, last review
* ``data/strategy_history.json``   - append-only list of every change

Both are written atomically (temp + rename) and git-ignored. ``config.yaml`` is never touched.
Precedence for the strategy a symbol trades with: review override > ``watchlist[].strategy`` > ``strategy.name``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from config.settings import Settings, WatchItem, data_path
from utils.jsonstore import read_json, write_json_atomic

RUNNING_STALE_AFTER = timedelta(minutes=30)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class ReviewStore:
    def __init__(self, overrides_path: str | Path, history_path: str | Path) -> None:
        self.overrides_path = Path(overrides_path)
        self.history_path = Path(history_path)

    @classmethod
    def from_settings(cls, settings: Settings) -> "ReviewStore":
        cfg = settings.strategy_review
        return cls(data_path(cfg.overrides_file), data_path(cfg.history_file))

    # ------------------------------------------------------------------ raw state
    def _load(self) -> dict[str, Any]:
        data = read_json(self.overrides_path, {})
        data = data if isinstance(data, dict) else {}
        for key in ("overrides", "pending"):
            if not isinstance(data.get(key), dict):
                data[key] = {}
        return data

    def _save(self, data: dict[str, Any]) -> None:
        write_json_atomic(self.overrides_path, data)

    # ------------------------------------------------------------------ overrides
    def override_for(self, symbol: str) -> str | None:
        entry = self._load()["overrides"].get(symbol.upper())
        return entry.get("strategy") if isinstance(entry, dict) else None

    def overrides(self) -> dict[str, dict[str, Any]]:
        return dict(self._load()["overrides"])

    def last_changed_at(self, symbol: str) -> datetime | None:
        entry = self._load()["overrides"].get(symbol.upper())
        return parse_ts(entry.get("changed_at")) if isinstance(entry, dict) else None

    def set_override(self, symbol: str, strategy: str, *, previous: str, changed_at: datetime) -> None:
        data = self._load()
        sym = symbol.upper()
        data["overrides"][sym] = {"strategy": strategy, "previous": previous,
                                  "changed_at": changed_at.astimezone(timezone.utc).isoformat(timespec="seconds")}
        data["pending"].pop(sym, None)
        self._save(data)

    # ------------------------------------------------------------------ pending ("รอเปลี่ยน")
    def pending(self) -> dict[str, dict[str, Any]]:
        return dict(self._load()["pending"])

    def set_pending(self, symbol: str, entry: dict[str, Any]) -> None:
        data = self._load()
        data["pending"][symbol.upper()] = entry
        self._save(data)

    def clear_pending(self, symbol: str) -> None:
        data = self._load()
        if data["pending"].pop(symbol.upper(), None) is not None:
            self._save(data)

    # ------------------------------------------------------------------ last review / running flag
    def last_review(self) -> dict[str, Any] | None:
        last = self._load().get("last_review")
        return last if isinstance(last, dict) else None

    def save_last_review(self, summary: dict[str, Any]) -> None:
        data = self._load()
        data["last_review"] = summary
        data["running_since"] = None
        self._save(data)

    def try_start(self, now: datetime | None = None) -> bool:
        """Mark a review as running. False if another one is (not stale) - guards double clicks across processes."""
        data = self._load()
        now = now or _now()
        since = parse_ts(data.get("running_since"))
        if since is not None and now - since < RUNNING_STALE_AFTER:
            return False
        data["running_since"] = now.astimezone(timezone.utc).isoformat(timespec="seconds")
        self._save(data)
        return True

    def finish(self) -> None:
        data = self._load()
        if data.get("running_since") is not None:
            data["running_since"] = None
            self._save(data)

    def is_running(self, now: datetime | None = None) -> bool:
        since = parse_ts(self._load().get("running_since"))
        return since is not None and (now or _now()) - since < RUNNING_STALE_AFTER

    # ------------------------------------------------------------------ history
    def history(self, limit: int | None = None) -> list[dict[str, Any]]:
        rows = read_json(self.history_path, [])
        rows = rows if isinstance(rows, list) else []
        return rows[-limit:] if limit else rows

    def append_history(self, entry: dict[str, Any]) -> None:
        write_json_atomic(self.history_path, [*self.history(), entry])


def effective_strategy(settings: Settings, store: ReviewStore | None, item: WatchItem) -> str:
    """The strategy a symbol trades with right now."""
    override = store.override_for(item.symbol) if store else None
    return override or item.strategy or settings.strategy.name
