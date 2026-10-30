#!/usr/bin/env python3
"""Backtest example: one strategy in detail, a parameter sweep, and a strategy comparison.

    python examples/backtest_example.py                 # offline (synthetic data)
    python examples/backtest_example.py --yahoo AAPL    # real data from Yahoo Finance
    python examples/backtest_example.py --yahoo PTT.BK --market TH

DISCLAIMER: education/research only - not investment advice.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402
from loguru import logger  # noqa: E402

from backtesting.runner import compare_strategies, run_backtest  # noqa: E402
from config.settings import EXAMPLE_CONFIG_PATH, load_settings  # noqa: E402
from data.providers import SyntheticProvider  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yahoo", metavar="SYMBOL", help="use Yahoo Finance data for SYMBOL")
    ap.add_argument("--market", default="US", choices=["US", "TH"])
    ap.add_argument("--period", default="2y")
    args = ap.parse_args()

    logger.remove()
    logger.add(sys.stderr, level="WARNING")
    settings = load_settings(EXAMPLE_CONFIG_PATH)

    if args.yahoo:
        from data.providers.yahoo import YahooProvider

        symbol = args.yahoo.upper()
        df = YahooProvider().fetch_history(symbol, interval="1d", period=args.period)
    else:
        symbol = "DEMO"
        df = SyntheticProvider(seed=7).fetch_history(symbol, interval="1d", period=args.period)
    print(f"Data: {symbol} {len(df)} daily bars {df.index[0]:%Y-%m-%d} -> {df.index[-1]:%Y-%m-%d}\n")

    # 1) Single strategy with custom parameters
    result = run_backtest(df, symbol, "rsi", settings, params={"period": 14, "oversold": 30, "overbought": 70},
                          market=args.market)
    print(result.summary())
    trades = result.trades_df()
    if not trades.empty:
        print("\nLast 5 trades:")
        print(trades[["entry_time", "entry_price", "exit_time", "exit_price", "quantity", "pnl", "exit_reason"]]
              .tail().to_string(index=False))

    # 2) Parameter sweep for MA crossover
    print("\nParameter sweep - ma_crossover (fast/slow):")
    rows = []
    for fast in (5, 10, 20):
        for slow in (30, 50, 100):
            r = run_backtest(df, symbol, "ma_crossover", settings, params={"fast_period": fast, "slow_period": slow},
                             market=args.market)
            m = r.metrics
            rows.append({"fast": fast, "slow": slow, "return_%": round(m.total_return_pct, 2),
                         "max_dd_%": round(m.max_drawdown_pct, 2), "sharpe": round(m.sharpe_ratio, 2),
                         "trades": m.num_trades, "win_%": round(m.win_rate_pct, 1)})
    print(pd.DataFrame(rows).sort_values("sharpe", ascending=False).to_string(index=False))
    print("  (beware of overfitting: the best in-sample parameters rarely stay best out-of-sample)")

    # 3) Compare all strategies with config parameters
    print("\nAll strategies:")
    print(compare_strategies(df, symbol, settings, market=args.market).to_string())
    print("\n⚠️  Backtests ignore many real-world effects. Not investment advice.")


if __name__ == "__main__":
    main()
