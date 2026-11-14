"""Emergency brake: stop opening NEW positions when the (paper) portfolio drawdown is too deep.

* ON  when drawdown >= ``trigger_drawdown_pct`` (default 10%)
* OFF when drawdown <  ``release_drawdown_pct`` (default 5%) or by hand (``python main.py resume``)
* Between the two levels the state does not change (hysteresis), so it cannot flap.
* Exits (signal sell, stop-loss, take-profit) are never blocked - the engine only asks :attr:`active`
  before an ENTRY.

State lives in a JSON file (atomic writes) so the scheduler, the dashboard and the CLI all see it.
After a manual ``resume`` the brake stays quiet until drawdown has first recovered below the release
level, otherwise it would re-trigger on the very next cycle. (The hard ``risk.max_drawdown_pct`` gate
in :class:`risk.manager.RiskManager` still applies.)
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from config.settings import EmergencyBrakeSettings, Settings, data_path
from utils.jsonstore import read_json, write_json_atomic

BrakeEvent = Literal["ON", "OFF"]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class EmergencyBrake:
    def __init__(self, cfg: EmergencyBrakeSettings, path: str | Path | None) -> None:
        self.cfg = cfg
        self.path = Path(path) if path else None
        self._mem: dict[str, Any] = {}

    @classmethod
    def from_settings(cls, settings: Settings) -> "EmergencyBrake":
        return cls(settings.emergency_brake, data_path(settings.emergency_brake.state_file))

    # ------------------------------------------------------------------ state
    def state(self) -> dict[str, Any]:
        raw = read_json(self.path, {}) if self.path else dict(self._mem)
        raw = raw if isinstance(raw, dict) else {}
        return {
            "active": bool(raw.get("active", False)) and self.cfg.enabled,
            "since": raw.get("since"),
            "reason": raw.get("reason"),
            "drawdown_pct": raw.get("drawdown_pct"),
            "suppressed": bool(raw.get("suppressed", False)),
            "last_release": raw.get("last_release"),
            "trigger_drawdown_pct": self.cfg.trigger_drawdown_pct,
            "release_drawdown_pct": self.cfg.release_drawdown_pct,
            "enabled": self.cfg.enabled,
        }

    @property
    def active(self) -> bool:
        return self.state()["active"]

    def _save(self, st: dict[str, Any]) -> None:
        keep = {k: st.get(k) for k in ("active", "since", "reason", "drawdown_pct", "suppressed", "last_release")}
        if self.path:
            write_json_atomic(self.path, keep)
        else:
            self._mem = keep

    # ------------------------------------------------------------------ transitions
    def update(self, drawdown_pct: float) -> BrakeEvent | None:
        """Feed the current drawdown (fraction, 0.10 = 10%). Returns "ON"/"OFF" only when the state flips."""
        if not self.cfg.enabled:
            return None
        st = self.state()
        trigger, release = self.cfg.trigger_drawdown_pct, self.cfg.release_drawdown_pct
        if not st["active"]:
            if st["suppressed"]:  # manual resume: re-arm once drawdown has recovered
                if drawdown_pct < release:
                    st["suppressed"] = False
                    self._save(st)
                return None
            if drawdown_pct >= trigger:
                st.update(active=True, since=_now_iso(), drawdown_pct=drawdown_pct,
                          reason=f"drawdown {drawdown_pct:.2%} >= {trigger:.0%}")
                self._save(st)
                return "ON"
            return None
        if drawdown_pct < release:
            st.update(active=False, drawdown_pct=drawdown_pct,
                      last_release={"at": _now_iso(), "by": "auto", "drawdown_pct": drawdown_pct})
            self._save(st)
            return "OFF"
        return None

    def resume(self, drawdown_pct: float | None = None) -> bool:
        """Manual release. Returns False if the brake was not active."""
        st = self.state()
        if not st["active"]:
            return False
        st.update(active=False, suppressed=True,
                  last_release={"at": _now_iso(), "by": "manual", "drawdown_pct": drawdown_pct})
        self._save(st)
        return True
