"""
Replikasi Python dari docs/Export_ML_Dataset.mq5 (blueprint Fase 2).

Perbedaan yang SENGAJA dari blueprint MQL5 (dokumentasi keputusan):
1. Kandidat sinyal diambil dari TrendReversalStrategy._decide_from_rows --
   SANGAT SAMA dengan yang jalan di backtest/live (satu sumber kebenaran),
   bukan threshold exporter MQ5 (ADX 20, RSI 30/70). Ini yang dimaksud
   "threshold konsisten": dataset mendeskripsikan distribusi sinyal yang
   benar-benar akan ditradingkan.
2. Label NET-OF-COST: kolom r_net = gross R dikurangi (fee + slippage
   kedua sisi + funding sesuai durasi hold), semuanya dalam satuan R
   (risiko = jarak SL = 2xATR). Kolom label_win tetap ada (gross, 1 = TP
   sebelum SL) supaya kompatibel dengan pipeline train_model.py blueprint.
3. hour/day_of_week dari timestamp UTC data (MQ5 pakai server time broker).

Format CSV = superset dari fixture docs/xauusd_ml_dataset.csv (20 kolom
asli + 4 kolom baru), jadi train_model.py blueprint tetap bisa jalan.

Jalankan:
    python -m src.ml.export_dataset --file data/BTCUSDT_15m_ext.csv --interval 15m
    python -m src.ml.export_dataset --file data/BTCUSDT_15m_ext.csv --interval 15m --resample-to 1h
"""

import argparse

import numpy as np
import pandas as pd

from src.backtest.param_sweep import _precompute_indicators
from src.backtest.run_backtest import load_candles
from src.risk.manager import RiskLimits
from src.strategy.trend_reversal import TrendReversalStrategy

FEE_RATE = 0.00035          # taker Hyperliquid per sisi
SLIPPAGE = 0.0002           # per sisi, konservatif
FUNDING_HOURLY = 0.000009   # mean |rate| BTC, Mar-Agu 2026
HORIZON = 30                # bar ke depan, sama dgn Export_ML_Dataset.mq5

FEATURE_COLS = [
    "dist_to_ema_atr",
    "adx_main", "adx_pdi", "adx_mdi", "adx_di_diff",
    "rsi",
    "atr_normalized",
    "body_atr", "upper_shadow_atr", "lower_shadow_atr",
    "hour", "day_of_week",
    "signal_type",
]

DATASET_COLS = [
    "time", "open", "high", "low", "close", "tick_volume",
    *FEATURE_COLS,
    "label_win",          # 1 = TP sebelum SL (gross, seperti MQ5)
    "outcome",            # "TP" | "SL" | "TIMEOUT"
    "bars_to_outcome",    # 1..horizon (TIMEOUT = horizon)
    "r_net",              # outcome gross dikurangi biaya, dalam satuan R
]


def _resample_candles(candles: list, src_minutes: int, dst_minutes: int) -> list:
    """Resample OHLCV (mis. 15m -> 1h). Asumsi: bar sumber lengkap (open_time t)."""
    if dst_minutes <= src_minutes:
        return candles
    df = pd.DataFrame(candles)
    for col in ("o", "h", "l", "c", "v"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["dt"] = pd.to_datetime(df["t"].astype("int64"), unit="ms", utc=True)
    df = df.set_index("dt").sort_index()
    agg = df.resample(f"{dst_minutes}min", label="left", closed="left").agg(
        {"o": "first", "h": "max", "l": "min", "c": "last", "v": "sum"}
    ).dropna(subset=["o", "c"])
    out = []
    for dt, row in agg.iterrows():
        t = int(dt.timestamp() * 1000)
        out.append({
            "t": t, "T": t + dst_minutes * 60_000 - 1,
            "o": float(row["o"]), "h": float(row["h"]),
            "l": float(row["l"]), "c": float(row["c"]),
            "v": float(row["v"]), "n": "",
        })
    return out


def load_funding(path: str):
    """CSV dari fetch_funding.py -> (times_ms, rates) terurut."""
    df = pd.read_csv(path)
    return df["time"].values.astype("int64"), df["funding_rate"].values.astype(float)


def _cost_r(entry_price, t_entry_ms, bars_held, interval_minutes,
            roundtrip_cost_frac, funding, funding_hourly, signal_type, sl_dist):
    """Biaya per trade dalam satuan R (fee+slippage + funding prorata durasi)."""
    hold_hours = bars_held * interval_minutes / 60.0
    if funding is not None:
        f_times, f_rates = funding
        t_end = t_entry_ms + int(bars_held * interval_minutes * 60_000)
        lo = int(np.searchsorted(f_times, t_entry_ms))
        hi = int(np.searchsorted(f_times, t_end))
        rate_sum = float(f_rates[lo:hi].sum())
        funding_usd = entry_price * (rate_sum if signal_type == 1 else -rate_sum)
    else:
        funding_usd = entry_price * funding_hourly * hold_hours
    return (entry_price * roundtrip_cost_frac + funding_usd) / sl_dist


# Trailing = RiskLimits default (src/risk/manager.py)
TRAIL_START = 1.5   # trailing aktif setelah profit >= 1.5 ATR
TRAIL_DIST = 1.2    # jarak SL baru dari harga terbaik
TRAIL_STEP = 0.3    # SL cuma digeser jika >= 0.3 ATR


def simulate_trailing_exit(candles, i_entry, signal_type, entry_price, atr,
                           n, horizon=HORIZON):
    """Simulasi eksekusi engine: SL/TP fixed + trailing stop.

    Urutan per bar SETIA src/backtest/engine.py:
      (1) exit-check pakai SL/TP yang berlaku (dibentuk dari bar-bar sebelumnya);
          SL dicek lebih dulu dari TP pada bar yang sama (konservatif);
      (2) update trailing pakai extreme bar INI (best_price), SL baru
          efektif untuk bar BERIKUTNYA (tidak look-ahead).
    Catatan: engine live tidak punya batas horizon, tapi di sini dipotong di
    `horizon` bar (exit market di close) -- mayoritas trade resolve < 30 bar;
    ini approximate utk konsistensi horizon dengan label fixed SL/TP.

    Return (exit_r_gross, bars_held).
    """
    sl_dist = 2.0 * atr
    tp_dist = sl_dist * 1.5
    sl_price = entry_price - sl_dist if signal_type == 1 else entry_price + sl_dist
    tp_price = entry_price + tp_dist if signal_type == 1 else entry_price - tp_dist
    trail_start = atr * TRAIL_START
    trail_dist = atr * TRAIL_DIST
    trail_step = atr * TRAIL_STEP
    best = entry_price
    for j in range(i_entry + 1, min(i_entry + horizon + 1, n)):
        bh, bl = float(candles[j]["h"]), float(candles[j]["l"])
        # --- (1) exit check: SL lebih dulu dari TP (konservatif, spt engine) ---
        if signal_type == 1:
            if bl <= sl_price:
                return (sl_price - entry_price) / sl_dist, j - i_entry
            if bh >= tp_price:
                return 1.5, j - i_entry
        else:
            if bh >= sl_price:
                return (entry_price - sl_price) / sl_dist, j - i_entry
            if bl <= tp_price:
                return 1.5, j - i_entry
        # --- (2) trailing update (efektif bar berikutnya, spt engine) ---
        if signal_type == 1:
            best = max(best, bh)
            if best - entry_price >= trail_start:
                new_sl = best - trail_dist
                if new_sl > sl_price + trail_step:
                    sl_price = new_sl
        else:
            best = min(best, bl)
            if entry_price - best >= trail_start:
                new_sl = best + trail_dist
                if new_sl < sl_price - trail_step:
                    sl_price = new_sl
    # timeout -> exit market di close (approximasi)
    j_end = min(i_entry + horizon, n - 1)
    ec = float(candles[j_end]["c"])
    move = (ec - entry_price) if signal_type == 1 else (entry_price - ec)
    return move / sl_dist, j_end - i_entry


def build_dataset(candles: list, strategy: TrendReversalStrategy,
                  interval_minutes: int, fee_rate: float = FEE_RATE,
                  slippage: float = SLIPPAGE,
                  funding_hourly: float = FUNDING_HOURLY,
                  entry_mode: str = "market",
                  fill_window: int = 12,
                  funding=None,
                  simulate_trailing: bool = False) -> pd.DataFrame:
    """Ekspor kandidat sinyal + 13 fitur + label net-of-cost.

    Kandidat  : TrendReversalStrategy._decide_from_rows (satu sumber kebenaran
                dengan backtest/live) pada bar i, memakai prev = bar i-1.
    Fitur     : replikasi 1:1 rumus Export_ML_Dataset.mq5 (semua /ATR).
    Label     : horizon 30 bar; SL dicek lebih dulu dari TP pada bar yang sama
                (konsisten & konservatif seperti MQ5). r_net mengurangi biaya.

    entry_mode:
      "market" -> entry di close bar sinyal (default, seperti blueprint MQ5);
                  biaya = fee taker 2 sisi + slippage 2 sisi.
      "limit"  -> maker-entry: pasang limit di close bar sinyal; fill HANYA
                  jika harga menyentuh level itu dalam `fill_window` bar ke
                  depan. Biaya entry = 0 (maker). Exit tetap diasumsikan
                  taker (SL stop-market) + slippage exit -> konservatif.
                  Kandidat yang tidak terisi di-skip (tidak jadi baris).
    """
    df = _precompute_indicators(candles, strategy)
    # fitur volume: rasio volume bar terhadap SMA20/SMA100 volume
    df["vol_sma20"] = df["v"].rolling(20).mean()
    df["vol_sma100"] = df["v"].rolling(100).mean()
    # fitur regime (iterasi-2): realized vol 50 bar + jarak ke high/low 500 bar
    df["rv_50"] = df["c"].pct_change().rolling(50).std()
    df["dist_hi_500_atr"] = (df["h"].rolling(500).max() - df["c"]) / df["atr"]
    df["dist_lo_500_atr"] = (df["c"] - df["l"].rolling(500).min()) / df["atr"]
    n = len(candles)
    rows = []
    # "market": fee+slippage kedua sisi; "limit": entry maker gratis, exit taker
    exit_cost_frac = fee_rate + slippage
    roundtrip_cost_frac = exit_cost_frac if entry_mode == "limit" else 2 * exit_cost_frac

    for i in range(strategy.required_bars(), n - HORIZON):
        candle = candles[i]
        cur_open, cur_high = float(candle["o"]), float(candle["h"])
        cur_low, cur_close = float(candle["l"]), float(candle["c"])
        last, prev = df.iloc[i], df.iloc[i - 1]

        sig_result = strategy._decide_from_rows(last, prev)
        if sig_result.signal.value == "HOLD":
            continue

        atr = float(last["atr"])
        if not atr == atr or atr <= 0 or cur_close <= 0:
            continue

        signal_type = 1 if sig_result.signal.value == "BUY" else 2
        dt = pd.Timestamp(int(candle["t"]), unit="ms", tz="UTC")

        # --- Entry sesuai mode ---
        if entry_mode == "limit":
            entry_price = cur_close
            fill_i = None
            for f in range(i + 1, min(i + 1 + fill_window, n - HORIZON)):
                touched = (float(candles[f]["l"]) <= entry_price if signal_type == 1
                           else float(candles[f]["h"]) >= entry_price)
                if touched:
                    fill_i = f
                    break
            if fill_i is None:
                continue  # tidak terisi -> bukan trade, skip
            i_entry = fill_i
        else:
            entry_price = cur_close
            i_entry = i

        # --- 13 fitur (rumus identik MQ5, dari bar sinyal i) ---
        features = {
            "dist_to_ema_atr": (cur_close - float(last["ema"])) / atr,
            "adx_main": float(last["adx"]),
            "adx_pdi": float(last["plus_di"]),
            "adx_mdi": float(last["minus_di"]),
            "adx_di_diff": float(last["plus_di"]) - float(last["minus_di"]),
            "rsi": float(last["rsi"]),
            "atr_normalized": atr / cur_close * 1000.0,
            "body_atr": abs(cur_close - cur_open) / atr,
            "upper_shadow_atr": (cur_high - max(cur_open, cur_close)) / atr,
            "lower_shadow_atr": (min(cur_open, cur_close) - cur_low) / atr,
            "hour": dt.hour,
            "day_of_week": dt.dayofweek,  # 0=Senin (MQ5: 1=Senin)
            "signal_type": signal_type,
        }
        # fitur tambahan (di luar 13 fitur MQ5; train_model memakai jika ada)
        v20, v100 = float(df["vol_sma20"].iloc[i]), float(df["vol_sma100"].iloc[i])
        vol = float(candle["v"])
        features["vol_ratio_20"] = vol / v20 if v20 > 0 else 1.0
        features["vol_ratio_100"] = vol / v100 if v100 > 0 else 1.0
        if funding is not None:
            t_i = int(candle["t"])
            f_idx = int(np.searchsorted(funding[0], t_i))
            features["funding_rate"] = float(
                funding[1][min(f_idx, len(funding[1]) - 1)]
            )
        # fitur regime (iterasi-2): realized vol 50 bar + jarak high/low 500 bar
        rv = float(df["rv_50"].iloc[i])
        dhi = float(df["dist_hi_500_atr"].iloc[i])
        dlo = float(df["dist_lo_500_atr"].iloc[i])
        if rv != rv or dhi != dhi or dlo != dlo:
            continue  # history regime belum cukup (500 bar pertama) -> skip
        features["rv_50"] = rv
        features["dist_hi_500_atr"] = dhi
        features["dist_lo_500_atr"] = dlo

        # --- Label: simulasi SL/TP ke depan (SL dicek dulu, seperti MQ5) ---
        sl_dist = 2.0 * atr                       # RiskLimits.atr_sl_mult
        tp_dist = sl_dist * 1.5                   # RiskLimits.tp_rr_ratio
        outcome, bars_to_outcome, gross_r = "TIMEOUT", HORIZON, None
        if signal_type == 1:
            sl_price, tp_price = entry_price - sl_dist, entry_price + tp_dist
        else:
            sl_price, tp_price = entry_price + sl_dist, entry_price - tp_dist

        for j in range(i_entry, min(i_entry + HORIZON + 1, n)):
            bar_high, bar_low = float(candles[j]["h"]), float(candles[j]["l"])
            if signal_type == 1:
                hit_sl, hit_tp = bar_low <= sl_price, bar_high >= tp_price
            else:
                hit_sl, hit_tp = bar_high >= sl_price, bar_low <= tp_price
            if hit_sl:   # dicek lebih dulu -> konservatif
                outcome, bars_to_outcome, gross_r = "SL", j - i_entry, -1.0
                break
            if hit_tp:
                outcome, bars_to_outcome, gross_r = "TP", j - i_entry, 1.5
                break
        if outcome == "TIMEOUT":
            j_end = min(i_entry + HORIZON, n - 1)
            exit_close = float(candles[j_end]["c"])
            bars_to_outcome = j_end - i_entry
            move = (exit_close - entry_price) if signal_type == 1 else (entry_price - exit_close)
            gross_r = move / sl_dist

        # --- Biaya dalam satuan R ---
        # fee+slippage atas notional ~ entry price. Funding: kalau array riil
        # disediakan, jumlahkan rate per jam selama hold (signed by direction:
        # long bayar rate positif, short menerima); kalau tidak, pakai mean.
        cost_r = _cost_r(entry_price, int(candles[i_entry]["t"]), bars_to_outcome,
                         interval_minutes, roundtrip_cost_frac, funding,
                         funding_hourly, signal_type, sl_dist)
        r_net = gross_r - cost_r

        row = {
            "time": dt.strftime("%Y-%m-%d %H:%M"),
            "open": cur_open, "high": cur_high, "low": cur_low, "close": cur_close,
            "tick_volume": float(candle["v"]),
            **features,
            "label_win": 1 if outcome == "TP" else 0,
            "outcome": outcome,
            "bars_to_outcome": bars_to_outcome,
            "r_net": round(r_net, 6),
        }
        if simulate_trailing:
            trail_r, trail_bars = simulate_trailing_exit(
                candles, i_entry, signal_type, entry_price, atr, n)
            cost_tr = _cost_r(entry_price, int(candles[i_entry]["t"]), trail_bars,
                              interval_minutes, roundtrip_cost_frac, funding,
                              funding_hourly, signal_type, sl_dist)
            # definisi win konsisten dgn engine: exit R > 0
            row["label_trail"] = 1 if trail_r > 0 else 0
            row["bars_trail"] = trail_bars
            row["r_trail_net"] = round(trail_r - cost_tr, 6)
        rows.append(row)

    cols = list(DATASET_COLS)
    for extra in ("vol_ratio_20", "vol_ratio_100", "funding_rate",
                  "rv_50", "dist_hi_500_atr", "dist_lo_500_atr",
                  "label_trail", "bars_trail", "r_trail_net"):
        if rows and extra in rows[0]:
            cols.append(extra)
    return pd.DataFrame(rows, columns=cols)


def main():
    ap = argparse.ArgumentParser(description="Ekspor dataset ML (replikasi Export_ML_Dataset.mq5)")
    ap.add_argument("--file", default="data/BTCUSDT_15m_ext.csv", help="CSV candle (t,o,h,l,c,v)")
    ap.add_argument("--symbol", default="BTC")
    ap.add_argument("--interval", default="15m", help="interval candle sumber")
    ap.add_argument("--resample-to", default=None, help="opsional: resample ke interval lebih besar (mis. 1h)")
    ap.add_argument("--out", default=None, help="path output CSV")
    ap.add_argument("--adx-strength", type=float, default=15.0)
    ap.add_argument("--rsi-oversold", type=float, default=35.0)
    ap.add_argument("--rsi-overbought", type=float, default=65.0)
    ap.add_argument("--entry", choices=["market", "limit"], default="market",
                    help="market = entry di close (taker); limit = maker-entry")
    ap.add_argument("--fill-window", type=int, default=12,
                    help="mode limit: bar ke depan utk menunggu fill")
    ap.add_argument("--funding-file", default=None,
                    help="CSV funding riil (fetch_funding.py); kosong = mean konstan")
    ap.add_argument("--simulate-trailing", action="store_true",
                    help="tambah label_trail/r_trail_net (simulasi trailing engine)")
    args = ap.parse_args()

    def _to_minutes(spec: str) -> int:
        spec = spec.strip().lower()
        if spec.endswith("h"):
            return int(spec[:-1]) * 60
        return int(spec.rstrip("m"))

    src_min = _to_minutes(args.interval)
    dst_min = _to_minutes(args.resample_to) if args.resample_to else src_min

    candles = load_candles(args.file)
    print(f"Loaded {len(candles)} candle dari {args.file} (interval sumber {args.interval})")
    if dst_min > src_min:
        candles = _resample_candles(candles, src_min, dst_min)
        print(f"Resampled -> {len(candles)} candle {args.resample_to}")

    strategy = TrendReversalStrategy(
        adx_strength=args.adx_strength,
        rsi_oversold=args.rsi_oversold,
        rsi_overbought=args.rsi_overbought,
        require_trend_alignment=False,
    )
    print(f"Parameter sumber: ADX>={strategy.adx_strength}, "
          f"RSI {strategy.rsi_oversold}/{strategy.rsi_overbought}, "
          f"SL=2xATR, TP=1.5R, horizon={HORIZON} bar, entry={args.entry}")

    funding = None
    if args.funding_file:
        funding = load_funding(args.funding_file)
        print(f"Funding riil dimuat: {len(funding[0])} rekam dari {args.funding_file}")

    ds = build_dataset(candles, strategy, interval_minutes=dst_min,
                       entry_mode=args.entry, fill_window=args.fill_window,
                       funding=funding, simulate_trailing=args.simulate_trailing)

    tag = f"{args.resample_to or args.interval}_{args.entry}".lower()
    out = args.out or f"data/{args.symbol}_{tag}_ml_dataset.csv".lower()
    ds.to_csv(out, index=False)

    if len(ds):
        tp_mean = ds[ds.outcome == "TP"]["r_net"].mean() if (ds.outcome == "TP").any() else float("nan")
        print(f"\nDataset tersimpan: {out} ({len(ds)} baris kandidat)")
        print(f"  BUY={int((ds.signal_type == 1).sum())} | SELL={int((ds.signal_type == 2).sum())}")
        wr_gross = ds["label_win"].mean() * 100
        timeout = (ds["outcome"] == "TIMEOUT").mean() * 100
        print(f"  WR gross (TP sebelum SL): {wr_gross:.1f}% | TIMEOUT: {timeout:.1f}%")
        print(f"  mean r_net: {ds['r_net'].mean():+.4f} R | median: {ds['r_net'].median():+.4f} R")
        print(f"  estimasi biaya per trade: {1.5 - tp_mean:+.4f} R (dari baris TP)")
        if args.simulate_trailing and "r_trail_net" in ds.columns:
            print(f"  TRAIL: mean r_trail_net {ds['r_trail_net'].mean():+.4f} R | "
                  f"WR trail {ds['label_trail'].mean() * 100:.1f}% | "
                  f"median bars {ds['bars_trail'].median():.0f}")


if __name__ == "__main__":
    main()
