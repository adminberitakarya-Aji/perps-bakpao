"""
Poin 1 roadmap go-live: validasi strategi+filter ML di data 1H ASLI
Hyperliquid (bukan Binance). Menutup catatan validitas venue: semua hasil
WF sebelumnya memakai harga Binance Vision.

    python -m src.backtest.run_ml_backtest

Membandingkan dua konfigurasi pada data yang sama:
  A. strategi mentah (tanpa filter ML)  -> E[r_net] negatif (dari WF)
  B. strategi + filter ML p>=0.60       -> E[r_net] +0.075R (dari WF)
Konsistensi B dengan WF di Binance = validasi venue lolos.
"""

import time

from src.backtest.engine import run_backtest, BacktestConfig
from src.backtest.run_backtest import load_candles
from src.ml.inference import MLSignalFilter
from src.risk.manager import RiskManager, RiskLimits
from src.strategy.trend_reversal import TrendReversalStrategy


def summarize(label: str, result, initial_equity: float):
    s = result.summary()
    print(f"{label}:")
    print(f"  trades={s['total_trades']} | WR={s['win_rate_pct']}% | "
          f"PF={s['profit_factor']} | return={s['total_return_pct']}% | "
          f"MDD={s['max_drawdown_pct']}% | avg/trade={s['avg_trade_pnl_usd']} USD | "
          f"trailing={s['trades_with_trailing']}")


def main():
    file_path = "data/BTC_1h.csv"
    candles = load_candles(file_path)
    first = time.strftime("%Y-%m-%d", time.gmtime(int(candles[0]["t"]) / 1000))
    last = time.strftime("%Y-%m-%d", time.gmtime(int(candles[-1]["t"]) / 1000))
    print(f"Data HL asli: {len(candles)} candle 1H ({first} s/d {last} UTC)")

    # Konfigurasi produksi = konfigurasi exporter ML (alignment OFF)
    strategy = TrendReversalStrategy(require_trend_alignment=False)
    config = BacktestConfig()

    # Model produksi: target label_trail (label = simulasi trailing engine,
    # net-of-cost) -- konsisten dengan eksekusi yang memakai trailing.
    ml = MLSignalFilter("models/btc_ml_trail_1h.onnx")

    # A. strategi mentah, trailing ON (baseline)
    result_a = run_backtest(
        candles, strategy, RiskManager(RiskLimits()), config)
    summarize("A. Tanpa filter ML (trailing ON)", result_a, config.initial_equity)

    # B. + filter ML trail-target, trailing ON = KONFIGURASI PRODUKSI
    result_b = run_backtest(
        candles, strategy, RiskManager(RiskLimits()), config,
        signal_filter=lambda window, sig: ml.allow(window, sig, strategy),
    )
    summarize(f"B. Filter ML trail p>={ml.threshold} (trailing ON) = PRODUKSI",
              result_b, config.initial_equity)

    # C. + filter ML trail, trailing OFF (diagnostik: kontribusi trailing)
    result_c = run_backtest(
        candles, strategy, RiskManager(RiskLimits(use_trailing=False)), config,
        signal_filter=lambda window, sig: ml.allow(window, sig, strategy),
    )
    summarize(f"C. Filter ML trail p>={ml.threshold} (trailing OFF)",
              result_c, config.initial_equity)


if __name__ == "__main__":
    main()
