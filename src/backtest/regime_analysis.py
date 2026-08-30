"""
Pisahkan performa strategi berdasarkan kondisi market saat entry: TRENDING
(ADX tinggi) vs SIDEWAYS (ADX rendah) -- bukan digabung rata-rata 6 bulan.

Kenapa ini penting: strategi yang "breakeven" secara agregat bisa saja
sebenarnya PROFITABLE di kondisi trending tapi RUGI di sideways (atau
sebaliknya) -- rata-rata gabungan menyembunyikan itu. Kalau ternyata
strategi cuma bagus di salah satu regime, itu actionable: tambah filter
regime sebelum entry, bukan cuma tuning parameter.

Regime diklasifikasi dari nilai ADX pada bar ENTRY tiap trade (bukan bar
acak) -- konsisten dengan bagaimana strategi sendiri menilai kekuatan trend
saat itu.

Jalankan:
    python -m src.backtest.regime_analysis --file data/BTC_1h.csv
"""

import argparse

from src.backtest.engine import run_backtest, BacktestConfig
from src.backtest.run_backtest import load_candles
from src.strategy.trend_reversal import TrendReversalStrategy
from src.risk.manager import RiskManager, RiskLimits


def label_regime(strategy, candles, trades, threshold):
    """Return list of (Trade, regime_str, adx_value)."""
    labeled = []
    for t in trades:
        window = candles[: t.entry_index + 1]
        df = strategy._to_df(window)
        df = strategy._compute_indicators(df)
        adx_at_entry = df["adx"].iloc[-1]
        regime = "trending" if adx_at_entry >= threshold else "sideways"
        labeled.append((t, regime, float(adx_at_entry)))
    return labeled


def summarize_subset(trades: list) -> dict:
    if not trades:
        return {"total_trades": 0, "note": "tidak ada trade di regime ini"}

    wins = [t for t in trades if t.pnl_usd > 0]
    losses = [t for t in trades if t.pnl_usd <= 0]
    gross_profit = sum(t.pnl_usd for t in wins)
    gross_loss = abs(sum(t.pnl_usd for t in losses))

    return {
        "total_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 1),
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss > 0 else float("inf"),
        "total_pnl_usd": round(sum(t.pnl_usd for t in trades), 2),
        "avg_pnl_usd": round(sum(t.pnl_usd for t in trades) / len(trades), 2),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True)
    parser.add_argument("--adx-strength", type=float, default=22.0, help="ADX minimum strategi (entry gate)")
    parser.add_argument("--regime-threshold", type=float, default=25.0, help="ambang ADX pemisah trending/sideways")
    parser.add_argument("--no-align", action="store_true", help="pakai require_trend_alignment=False")
    args = parser.parse_args()

    candles = load_candles(args.file)
    strategy = TrendReversalStrategy(
        adx_strength=args.adx_strength,
        require_trend_alignment=not args.no_align,
    )
    risk_manager = RiskManager(RiskLimits())
    config = BacktestConfig()

    result = run_backtest(candles, strategy, risk_manager, config)
    print(f"Total trade (semua regime): {len(result.trades)}")
    print(f"Ringkasan gabungan: {result.summary()}")

    labeled = label_regime(strategy, candles, result.trades, args.regime_threshold)
    trending_trades = [t for t, regime, _ in labeled if regime == "trending"]
    sideways_trades = [t for t, regime, _ in labeled if regime == "sideways"]

    print(f"\n=== TRENDING saat entry (ADX >= {args.regime_threshold}) ===")
    print(summarize_subset(trending_trades))

    print(f"\n=== SIDEWAYS saat entry (ADX < {args.regime_threshold}) ===")
    print(summarize_subset(sideways_trades))

    print(
        "\nCatatan: max_drawdown per-regime TIDAK dihitung di sini (butuh "
        "equity curve kontinu, tercampur regime lain) -- angka di atas fokus "
        "ke profitabilitas per trade, bukan risiko jalur equity."
    )


if __name__ == "__main__":
    main()
    