"""
Jalankan backtest dari file CSV hasil fetch_historical.py.

    python -m src.backtest.run_backtest --file data/BTC_1h.csv
"""

import argparse
import csv

from src.backtest.engine import run_backtest, BacktestConfig
from src.strategy.trend_reversal import TrendReversalStrategy
from src.risk.manager import RiskManager, RiskLimits


def load_candles(path: str) -> list:
    candles = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            candles.append({
                "t": row["t"], "T": row["T"],
                "o": row["o"], "h": row["h"], "l": row["l"], "c": row["c"],
                "v": row.get("v", 0), "n": row.get("n", 0),
            })
    return candles


def print_summary(label: str, summary: dict):
    print(f"\n--- {label} ---")
    for k, v in summary.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True, help="path ke CSV hasil fetch_historical.py")
    parser.add_argument("--initial-equity", type=float, default=1000.0)
    parser.add_argument("--leverage", type=float, default=1.0)
    args = parser.parse_args()

    candles = load_candles(args.file)
    print(f"Memuat {len(candles)} candle dari {args.file}")

    config = BacktestConfig(initial_equity=args.initial_equity, leverage=args.leverage)
    risk_manager = RiskManager(RiskLimits())

    # bandingkan dua mode require_trend_alignment sekaligus
    for aligned in (True, False):
        strategy = TrendReversalStrategy(require_trend_alignment=aligned)
        result = run_backtest(candles, strategy, risk_manager, config)
        label = f"require_trend_alignment={aligned}"
        print_summary(label, result.summary())
