import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_trend_reversal import make_trending_candles, make_downtrending_candles, make_flat_candles
from src.backtest.engine import run_backtest, BacktestConfig
from src.strategy.trend_reversal import TrendReversalStrategy
from src.risk.manager import RiskManager, RiskLimits


def run_scenario(name, candles, aligned):
    strategy = TrendReversalStrategy(require_trend_alignment=aligned)
    risk_manager = RiskManager(RiskLimits())
    config = BacktestConfig(initial_equity=1000.0, min_bars=52)

    result = run_backtest(candles, strategy, risk_manager, config)
    summary = result.summary()
    print(f"[{name} | aligned={aligned}] {summary}")

    # sanity checks dasar
    assert result.final_equity > 0 or summary["total_trades"] == 0, "equity tidak boleh negatif tak terbatas"
    for t in result.trades:
        assert t.exit_index >= t.entry_index, "exit tidak boleh sebelum entry"
        assert t.size_usd > 0, "size posisi harus positif"
    print(f"  -> {len(result.trades)} trade, sanity check OK\n")


if __name__ == "__main__":
    print("=== Backtest engine sanity test ===\n")

    run_scenario("uptrend", make_trending_candles(n=300, seed=10), aligned=True)
    run_scenario("uptrend", make_trending_candles(n=300, seed=10), aligned=False)
    run_scenario("downtrend", make_downtrending_candles(n=300, seed=11), aligned=True)
    run_scenario("flat", make_flat_candles(n=300, seed=12), aligned=True)

    print("Semua skenario selesai tanpa exception -> engine aman dipakai dengan data CSV asli.")
