"""
Baseline backtest rule-based 'setia ke parameter sumber' (arsitektur EA
XAU Phase 1 yang sudah terekstrak, TANPA lapisan ML):

  EMA 50, ADX period 14, threshold ADX 15, RSI 14 (35/65), ATR 14,
  SL = 2xATR, TP = 1.5R (TP_ATR_RR), reversal TANPA filter trend, mode both.

Dua hal yang diukur:
1. Statistik kandidat ala dataset ML (Export_ML_Dataset.mq5): tiap bar
   kandidat sinyal disimulasikan SL/TP ke depan (horizon 30 bar) ->
   WR dasar + expectancy dalam R, net-of-cost. Ini yang menentukan apakah
   lapisan ML wajib: break-even di RR 1.5 = 40%; di XAU WR dasarnya 33.7%.
2. Backtest engine (dengan sizing risiko 1%, fee, funding, kill switch)
   untuk varian ADX/alignment sebagai konteks.

Jalankan:
    python -m src.backtest.run_baseline --file data/BTCUSDT_15m_ext.csv --interval 15m
"""

import argparse

import numpy as np

from src.backtest.engine import BacktestConfig
from src.backtest.param_sweep import _precompute_indicators, _run_backtest_precomputed
from src.backtest.run_backtest import load_candles, print_summary
from src.risk.manager import RiskManager, RiskLimits
from src.strategy.base import Signal
from src.strategy.trend_reversal import TrendReversalStrategy

FEE_RATE = 0.00035          # taker Hyperliquid per sisi
SLIPPAGE = 0.0002           # per sisi, konservatif utk 15m market order
FUNDING_HOURLY = 0.000009   # mean |rate| BTC, Mar-Agu 2026 (per jam)
HORIZON = 30                # bar ke depan, sama dgn Export_ML_Dataset.mq5
WARMUP = 60                 # bar; >= required_bars strategi (52)


def forward_candidate_stats(df, strategy) -> dict:
    """Replicasi labeling ala Export_ML_Dataset.mq5: tiap bar kandidat
    sinyal, simulasi SL (2xATR) / TP (1.5R) ke depan (horizon 30 bar).
    Kalau dalam satu bar SL dan TP dua-duanya tersentuh, dihitung LOSS
    (asumsi konservatif yang sama dengan backtest engine)."""
    highs = df["h"].values
    lows = df["l"].values
    closes = df["c"].values
    atrs = df["atr"].values
    n = len(df)

    candidates = 0
    wins = 0
    gross_r = 0.0
    timeouts = 0
    reasons = {"FOLLOW": 0, "REVERSAL": 0}

    for i in range(WARMUP, n - 1):
        last, prev = df.iloc[i], df.iloc[i - 1]
        sig = strategy._decide_from_rows(last, prev)
        if sig.signal == Signal.HOLD:
            continue
        atr = atrs[i]
        if atr != atr or atr <= 0:  # NaN / invalid
            continue
        entry = closes[i]
        sl_dist = 2.0 * atr
        tp_dist = 1.5 * sl_dist
        is_buy = sig.signal == Signal.BUY

        candidates += 1
        key = "FOLLOW" if "FOLLOW" in sig.reason else "REVERSAL"
        reasons[key] += 1

        res_r = None
        for j in range(i + 1, min(i + 1 + HORIZON, n)):
            if is_buy:
                hit_sl = lows[j] <= entry - sl_dist
                hit_tp = highs[j] >= entry + tp_dist
            else:
                hit_sl = highs[j] >= entry + sl_dist
                hit_tp = lows[j] <= entry - tp_dist
            if hit_sl:  # dicek dulu -> konservatif
                res_r = -1.0
                break
            if hit_tp:
                res_r = 1.5
                break
        if res_r is None:  # timeout: exit market di akhir horizon
            timeouts += 1
            j = min(i + HORIZON, n - 1)
            move = (closes[j] - entry) / sl_dist * (1 if is_buy else -1)
            res_r = move

        gross_r += res_r
        if res_r > 0:
            wins += 1

    if candidates == 0:
        return {"candidates": 0}

    avg_gross = gross_r / candidates
    # biaya dalam satuan R: (fee + slippage) x2 sisi, dinormalisasi jarak
    # SL rata-rata (dalam % harga) -- approx pakai rata-rata 2*ATR.
    mean_sl_frac = float(np.nanmean(2.0 * atrs[WARMUP:] / closes[WARMUP:]))
    cost_r = (2 * FEE_RATE + 2 * SLIPPAGE) / mean_sl_frac
    avg_net = avg_gross - cost_r
    # break-even: WR*(1.5-c) = (1-WR)*(1+c)  ->  WR* = (1+c)/2.5
    breakeven_wr = (1.0 + cost_r) / 2.5 * 100

    return {
        "candidates": candidates,
        "follow": reasons["FOLLOW"],
        "reversal": reasons["REVERSAL"],
        "win_rate_pct": round(wins / candidates * 100, 1),
        "avg_R_gross": round(avg_gross, 4),
        "cost_R_per_trade": round(cost_r, 4),
        "avg_R_net": round(avg_net, 4),
        "breakeven_WR_pct": round(breakeven_wr, 1),
        "timeout_pct": round(timeouts / candidates * 100, 1),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True)
    parser.add_argument("--interval", default="15m", help="dipakai utk skala funding per bar")
    parser.add_argument("--adx", type=float, nargs="+", default=[15.0, 22.0])
    args = parser.parse_args()

    candles = load_candles(args.file)
    print(f"Memuat {len(candles)} candle dari {args.file}")

    unit = args.interval[-1]
    minutes = int(args.interval[:-1]) * {"m": 1, "h": 60, "d": 1440}[unit]
    funding_per_bar = FUNDING_HOURLY * minutes / 60

    # ---------- 1. statistik kandidat (setia ke sumber: ADX 15, unaligned) ----------
    base = TrendReversalStrategy(adx_strength=args.adx[0], require_trend_alignment=False)
    df = _precompute_indicators(candles, base)
    print(f"\n=== STATISTIK KANDIDAT (setia ke sumber: ADX={args.adx[0]:g}, "
          f"reversal tanpa filter trend, SL 2xATR, TP 1.5R, horizon {HORIZON} bar) ===")
    stats = forward_candidate_stats(df, base)
    for k, v in stats.items():
        print(f"  {k}: {v}")
    print(f"  (break-even WR di RR 1.5 = 40% sebelum biaya; di sini {stats.get('breakeven_WR_pct')}% "
          f"setelah biaya -- WR di bawahnya = rule-based rugi -> ML wajib)")

    # ---------- 2. backtest engine utk varian ADX/alignment ----------
    for adx in args.adx:
        for aligned in (False, True):
            strategy = TrendReversalStrategy(adx_strength=adx, require_trend_alignment=aligned)
            # RiskManager instance BARU per varian (daily kill switch itu stateful)
            risk_manager = RiskManager(RiskLimits())
            config = BacktestConfig(funding_rate_per_bar=funding_per_bar)
            # _run_backtest_precomputed langsung mengembalikan dict summary
            summary = _run_backtest_precomputed(candles, df, strategy, risk_manager, config)
            label = f"ADX={adx:g}, require_trend_alignment={aligned}"
            print_summary(label, summary)


if __name__ == "__main__":
    main()
