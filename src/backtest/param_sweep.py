"""
Parameter sweep: ADX strength, TP:SL ratio (RR), SL ATR multiplier.

PERINGATAN OVERFITTING -- baca ini sebelum percaya hasilnya:
Menjalankan banyak kombinasi parameter di data yang SAMA lalu memilih yang
profit factor-nya tertinggi itu rawan "menemukan" kombinasi yang kebetulan
cocok ke histori tertentu (multiple comparison problem) -- bukan edge
sungguhan. Semakin banyak kombinasi dicoba, semakin besar peluang salah
satunya kelihatan bagus murni karena kebetulan statistik.

Mitigasi yang dipakai script ini (minimal, bukan solusi sempurna):
- Data dibagi IN-SAMPLE (buat cari kombinasi terbaik) dan OUT-OF-SAMPLE
  (buat validasi -- kombinasi ini TIDAK PERNAH dilihat saat pencarian)
- Kombinasi yang bagus di in-sample tapi jelek/beda jauh di out-of-sample
  adalah tanda overfitting -- ABAIKAN kombinasi seperti itu, jangan pakai
- Minimal jumlah trade per kombinasi (MIN_TRADES) supaya tidak menilai dari
  sample yang terlalu kecil untuk bermakna secara statistik

Ini tetap bukan jaminan -- out-of-sample cuma satu periode, bukan multiple
walk-forward window. Untuk keyakinan lebih tinggi, ulangi split di titik
waktu yang beda-beda dan lihat apakah kombinasi yang sama tetap menang.

Jalankan:
    python -m src.backtest.param_sweep --file data/BTC_1h.csv
"""

import argparse
import itertools

from src.backtest.engine import BacktestConfig, MAX_LOOKBACK, Trade, run_backtest
from src.backtest.run_backtest import load_candles
from src.strategy.trend_reversal import TrendReversalStrategy
from src.strategy.base import Signal
from src.risk.manager import RiskManager, RiskLimits

MIN_TRADES = 15  # abaikan kombinasi dengan trade lebih sedikit dari ini


def _precompute_indicators(candles: list, base_strategy: TrendReversalStrategy):
    """Hitung indikator SEKALI untuk seluruh candle series. Semua kombinasi
    di sweep memakai ema/adx/rsi/atr_period yang SAMA (cuma beda threshold
    seperti adx_strength/tp_rr_ratio) -- jadi hasil ini valid dipakai ulang
    untuk semua kombinasi, tanpa recompute per kombinasi. Ini yang membuat
    sweep 100+ kombinasi feasible (dari puluhan menit -> hitungan detik)."""
    df = base_strategy._to_df(candles)
    df = base_strategy._compute_indicators(df)
    return df


def _run_backtest_precomputed(candles, df, strategy, risk_manager, config):
    """Sama seperti engine.run_backtest(), tapi pakai df indikator yang
    SUDAH dihitung (lewat _precompute_indicators) alih-alih recompute per
    bar. Logika exit/trailing/sizing/fee/funding disalin identik dari
    engine.run_backtest() -- kalau engine.py berubah, sinkronkan juga di sini."""
    equity = config.initial_equity
    equity_curve = [(config.min_bars, equity)]
    trades = []
    open_position = None

    for i in range(config.min_bars, len(candles)):
        candle = candles[i]
        high, low = float(candle["h"]), float(candle["l"])

        if open_position is not None:
            open_position["bars_held"] = i - open_position["entry_index"]

        if open_position is not None:
            hit_sl = hit_tp = False
            if open_position["signal"] == Signal.BUY:
                hit_sl = low <= open_position["sl"]
                hit_tp = high >= open_position["tp"]
            else:
                hit_sl = high >= open_position["sl"]
                hit_tp = low <= open_position["tp"]

            if hit_sl or hit_tp:
                exit_price = open_position["sl"] if hit_sl else open_position["tp"]
                exit_reason = "SL" if hit_sl else "TP"
                pnl = _compute_pnl(open_position, exit_price, config)
                equity += pnl
                trades.append(Trade(
                    entry_index=open_position["entry_index"], exit_index=i,
                    signal=open_position["signal"], entry_price=open_position["entry_price"],
                    exit_price=exit_price, size_usd=open_position["size_usd"], pnl_usd=pnl,
                    exit_reason=exit_reason, trailing_triggered=open_position.get("trailing_triggered", False),
                ))
                open_position = None

        if open_position is not None:
            best_price = high if open_position["signal"] == Signal.BUY else low
            new_sl = risk_manager.compute_trailing_sl(
                signal=open_position["signal"], entry_price=open_position["entry_price"],
                current_price=best_price, current_sl=open_position["sl"], entry_atr=open_position["atr"],
            )
            if new_sl is not None:
                open_position["sl"] = new_sl
                open_position["trailing_triggered"] = True

        if open_position is None and i >= 1:
            last, prev = df.iloc[i], df.iloc[i - 1]
            sig_result = strategy._decide_from_rows(last, prev)

            if sig_result.signal != Signal.HOLD:
                atr = float(last["atr"]) if last["atr"] == last["atr"] else None
                sl_distance_pct = None
                if atr is not None and atr > 0 and float(candle["c"]) > 0:
                    sl_distance_pct = (atr * risk_manager.limits.atr_sl_mult) / float(candle["c"])
                size_usd = risk_manager.check_and_size(
                    equity, sig_result.signal, sig_result.confidence, sl_distance_pct=sl_distance_pct
                )
                if size_usd > 0 and atr is not None and atr > 0:
                    entry_price = float(candle["c"])
                    sl, tp = risk_manager.compute_sl_tp(sig_result.signal, entry_price, atr)
                    open_position = {
                        "signal": sig_result.signal, "entry_price": entry_price, "sl": sl, "tp": tp,
                        "atr": atr, "size_usd": size_usd * config.leverage, "entry_index": i,
                        "bars_held": 0, "trailing_triggered": False,
                    }

        equity_curve.append((i, equity))

    if open_position is not None:
        last_close = float(candles[-1]["c"])
        open_position["bars_held"] = (len(candles) - 1) - open_position["entry_index"]
        pnl = _compute_pnl(open_position, last_close, config)
        equity += pnl
        trades.append(Trade(
            entry_index=open_position["entry_index"], exit_index=len(candles) - 1,
            signal=open_position["signal"], entry_price=open_position["entry_price"],
            exit_price=last_close, size_usd=open_position["size_usd"], pnl_usd=pnl,
            exit_reason="end_of_data", trailing_triggered=open_position.get("trailing_triggered", False),
        ))

    return _summarize(trades, equity_curve, equity)


def _compute_pnl(position: dict, exit_price: float, config: BacktestConfig) -> float:
    entry_price = position["entry_price"]
    size_usd = position["size_usd"]
    direction = 1 if position["signal"] == Signal.BUY else -1
    price_change_pct = (exit_price - entry_price) / entry_price
    gross_pnl = size_usd * price_change_pct * direction
    entry_fee = size_usd * config.fee_rate
    exit_fee = size_usd * config.fee_rate
    bars_held = max(0, position.get("bars_held", 1))
    funding_cost = size_usd * config.funding_rate_per_bar * bars_held
    return gross_pnl - entry_fee - exit_fee - funding_cost


def _summarize(trades: list, equity_curve: list, final_equity: float) -> dict:
    if not trades:
        return {"total_trades": 0}
    wins = [t for t in trades if t.pnl_usd > 0]
    losses = [t for t in trades if t.pnl_usd <= 0]
    gross_profit = sum(t.pnl_usd for t in wins)
    gross_loss = abs(sum(t.pnl_usd for t in losses))
    equity_values = [e for _, e in equity_curve]
    peak = equity_values[0]
    max_dd = 0.0
    for e in equity_values:
        peak = max(peak, e)
        max_dd = max(max_dd, (peak - e) / peak if peak > 0 else 0)
    return {
        "total_trades": len(trades),
        "wins": len(wins), "losses": len(losses),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 1),
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss > 0 else float("inf"),
        "total_return_pct": round((final_equity - equity_values[0]) / equity_values[0] * 100, 2),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "final_equity": round(final_equity, 2),
        "avg_trade_pnl_usd": round(sum(t.pnl_usd for t in trades) / len(trades), 2),
        "trades_with_trailing": sum(1 for t in trades if t.trailing_triggered),
    }


def run_sweep(candles, adx_values, rr_values, sl_mult_values, aligned_values, config):
    # Semua kombinasi pakai ema/adx/rsi/atr_period default yang sama -> hitung
    # indikator SEKALI di sini, dipakai ulang untuk semua kombinasi di bawah.
    base_strategy = TrendReversalStrategy()
    df = _precompute_indicators(candles, base_strategy)

    results = []
    for adx, rr, sl_mult, aligned in itertools.product(
        adx_values, rr_values, sl_mult_values, aligned_values
    ):
        strategy = TrendReversalStrategy(adx_strength=adx, require_trend_alignment=aligned)
        limits = RiskLimits(tp_rr_ratio=rr, atr_sl_mult=sl_mult)
        risk_manager = RiskManager(limits)
        summary = _run_backtest_precomputed(candles, df, strategy, risk_manager, config)
        summary.update({
            "adx_strength": adx, "tp_rr_ratio": rr,
            "atr_sl_mult": sl_mult, "aligned": aligned,
        })
        results.append(summary)
    return results


def fmt(r: dict) -> str:
    return (
        f"adx={r['adx_strength']:<4} rr={r['tp_rr_ratio']:<4} sl_mult={r['atr_sl_mult']:<4} "
        f"aligned={str(r['aligned']):<5} | trades={r.get('total_trades', 0):<4} "
        f"pf={r.get('profit_factor', 0):<6} return={r.get('total_return_pct', 0):<7}% "
        f"dd={r.get('max_drawdown_pct', 0)}%"
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True)
    parser.add_argument("--split", type=float, default=0.6, help="fraksi data untuk in-sample")
    parser.add_argument("--top-n", type=int, default=10)
    args = parser.parse_args()

    candles = load_candles(args.file)
    split_idx = int(len(candles) * args.split)
    in_sample = candles[:split_idx]
    out_sample = candles[split_idx:]

    print(f"Total {len(candles)} candle -> in-sample {len(in_sample)}, out-of-sample {len(out_sample)}")

    adx_values = [18, 20, 22, 25, 28, 30]
    rr_values = [1.0, 1.5, 2.0, 2.5, 3.0]
    sl_mult_values = [1.5, 2.0, 2.5]
    aligned_values = [True, False]
    total_combos = len(adx_values) * len(rr_values) * len(sl_mult_values) * len(aligned_values)

    config = BacktestConfig()

    print(f"\nMenjalankan {total_combos} kombinasi di IN-SAMPLE...")
    in_results = run_sweep(in_sample, adx_values, rr_values, sl_mult_values, aligned_values, config)

    valid = [r for r in in_results if r.get("total_trades", 0) >= MIN_TRADES]
    skipped = total_combos - len(valid)
    valid.sort(key=lambda r: r.get("profit_factor", 0), reverse=True)

    print(f"({skipped} kombinasi diabaikan karena trade < {MIN_TRADES})")
    print(f"\n=== TOP {args.top_n} kombinasi (IN-SAMPLE) ===")
    for r in valid[: args.top_n]:
        print(fmt(r))

    print(f"\n=== Validasi TOP 5 di OUT-OF-SAMPLE (data yang TIDAK dilihat saat sweep) ===")
    for r in valid[:5]:
        strategy = TrendReversalStrategy(adx_strength=r["adx_strength"], require_trend_alignment=r["aligned"])
        limits = RiskLimits(tp_rr_ratio=r["tp_rr_ratio"], atr_sl_mult=r["atr_sl_mult"])
        risk_manager = RiskManager(limits)
        oos_result = run_backtest(out_sample, strategy, risk_manager, config)
        oos_summary = oos_result.summary()
        oos_summary.update({
            "adx_strength": r["adx_strength"], "tp_rr_ratio": r["tp_rr_ratio"],
            "atr_sl_mult": r["atr_sl_mult"], "aligned": r["aligned"],
        })
        print(f"\n  IN : {fmt(r)}")
        print(f"  OOS: {fmt(oos_summary)}")
        pf_in = r.get("profit_factor", 0)
        pf_oos = oos_summary.get("profit_factor", 0)
        if isinstance(pf_in, (int, float)) and isinstance(pf_oos, (int, float)) and pf_in > 0:
            drop = (pf_in - pf_oos) / pf_in * 100
            flag = " <-- turun signifikan, WASPADA overfitting" if drop > 30 else ""
            print(f"  profit_factor turun {drop:.0f}% dari in-sample ke out-of-sample{flag}")


if __name__ == "__main__":
    main()
    