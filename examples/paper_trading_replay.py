#!/usr/bin/env python3
"""Paper-trading replay: run the *real* trading engine day by day over past data.

Unlike the vectorised backtester this exercises the full production path -
collector -> DB -> strategy -> risk -> paper broker -> orders/trades/snapshots -
using a simulated clock. Results go to ``data/replay.db`` so you can open the
dashboard on them:

    python examples/paper_trading_replay.py --days 120
    python main.py -c config/config.demo.yaml dashboard   # (set database.url to the replay db)

Uses offline synthetic prices by default; ``--yahoo`` uses real Yahoo data.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402
from loguru import logger  # noqa: E402

from config.settings import load_settings  # noqa: E402
from data.providers.base import DataProvider  # noqa: E402
from database import repository as repo  # noqa: E402
from database.models import Trade, TradeStatus  # noqa: E402
from database.session import Database  # noqa: E402
from execution.broker import PaperBroker  # noqa: E402
from execution.costs import CostModel  # noqa: E402
from execution.engine import TradingEngine  # noqa: E402
from monitoring.alerts import AlertManager  # noqa: E402
from monitoring.notifier import ConsoleNotifier  # noqa: E402


class ReplayProvider(DataProvider):
    """Serves pre-downloaded bars, hiding everything after the simulated 'now'."""

    name = "replay"

    def __init__(self, frames: dict[str, pd.DataFrame]) -> None:
        self.frames = frames
        self.now: datetime = datetime.min

    def fetch_history(self, symbol, interval="1d", period=None, start=None, end=None) -> pd.DataFrame:
        df = self.frames[symbol]
        df = df[df.index <= pd.Timestamp(self.now)]
        return df[df.index >= pd.Timestamp(start)] if start is not None else df


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=120, help="trading days to replay")
    ap.add_argument("--config", default=str(ROOT / "config" / "config.demo.yaml"))
    ap.add_argument("--yahoo", action="store_true", help="download real data from Yahoo instead of synthetic")
    ap.add_argument("--db", default=str(ROOT / "data" / "replay.db"))
    args = ap.parse_args()

    logger.remove()
    logger.add(sys.stderr, level="WARNING")
    settings = load_settings(args.config)
    settings.alerts.notify_signals = False  # keep output short

    Path(args.db).unlink(missing_ok=True)
    db = Database(f"sqlite:///{args.db}")
    db.create_all()

    if args.yahoo:
        from data.providers.yahoo import YahooProvider

        source: DataProvider = YahooProvider()
    else:
        from data.providers.synthetic import SyntheticProvider

        source = SyntheticProvider(seed=3)
    frames = {w.symbol: source.fetch_history(w.symbol, interval="1d", period="2y") for w in settings.watchlist}
    calendar = sorted(set().union(*(f.index for f in frames.values())))
    replay_days = calendar[-args.days:]

    provider = ReplayProvider(frames)
    clock = {"now": replay_days[0].to_pydatetime()}
    alerts = AlertManager(settings.alerts, ConsoleNotifier(), db)
    engine = TradingEngine(settings, db, provider, PaperBroker(CostModel.from_settings(settings.execution)),
                           alerts, clock=lambda: clock["now"])

    print(f"Replaying {len(replay_days)} trading days "
          f"({replay_days[0]:%Y-%m-%d} -> {replay_days[-1]:%Y-%m-%d}) on {len(frames)} symbols ...")
    for day in replay_days:
        # Run "after the close": bar timestamp + a few hours.
        clock["now"] = day.to_pydatetime() + timedelta(hours=8)
        provider.now = day.to_pydatetime()
        report = engine.run_cycle()
        for sym in report.symbols:
            if any(word in sym.action for word in ("BOUGHT", "SOLD")):
                print(f"  {day:%Y-%m-%d} {sym.symbol:<6} {sym.action}")

    with db.session() as s:
        pf = engine.portfolio(s)
        closed = [t for t in s.query(Trade).filter(Trade.status == TradeStatus.CLOSED)]
        wins = [t for t in closed if t.pnl > 0]
        print("\n=== Paper replay result ===")
        print(f"Equity      : {pf.initial_capital:,.2f} -> {pf.equity:,.2f} "
              f"({(pf.equity / pf.initial_capital - 1) * 100:+.2f}%)")
        print(f"Realized PnL: {pf.realized_pnl:+,.2f} | closed trades {len(closed)} | "
              f"win rate {len(wins) / len(closed) * 100 if closed else 0:.1f}% | open {len(repo.open_trades(s, pf.id))}")
        print(f"Peak / DD   : {pf.peak_equity:,.2f} / {pf.drawdown_pct:.2%}")
        print("\nMonthly PnL:")
        print(repo.period_pnl(s, pf, "M").to_string(index=False))
    print(f"\nAll orders, trades, signals and snapshots are in {args.db}")


if __name__ == "__main__":
    main()
