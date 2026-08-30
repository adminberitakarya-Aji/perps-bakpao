# Integrasi ONNX ke EA — Konfigurasi Produksi (TERKUNCI)

Hasil walk-forward 3 tahun (5 fold anchored-expanding, purge 30 bar, biaya riil)
pada BTC 1H: **p≥0,60 → E[r_net] +0,075 R over 835 trade, WR net 49,7% vs
break-even 44,4%, 5/5 fold di atas BE**. Model produksi dilatih di SELURUH data
(18.151 kandidat, Agu 2023–Agu 2026).

## Artefak

| File | Isi |
|---|---|
| `models/btc_ml_rf_1h.onnx` | RandomForest (150 tree, depth 6), opset 12, input `float_input` float32 `[None,16]` |
| `models/btc_ml_rf_1h.meta.json` | Urutan fitur, threshold, konfigurasi risiko, referensi WF |
| Regenerasi | `python -m src.ml.export_onnx --dataset data/btc_1h3y_ml_dataset.csv` |

Paritas sklearn vs onnxruntime terverifikasi: `max|diff| = 1.9e-07` (gate deploy: > 1e-5 = gagal).

## Spesifikasi I/O ONNX (MQL5)

- Input: `float_input`, matriks float32 `[N,16]` — **urutan kolom = `feature_order`
  di meta.json**, jangan diubah:
  `dist_to_ema_atr, adx_main, adx_pdi, adx_mdi, adx_di_diff, rsi, atr_normalized,
  body_atr, upper_shadow_atr, lower_shadow_atr, hour, day_of_week, signal_type,
  vol_ratio_20, vol_ratio_100, funding_rate`
- Output (zipmap OFF): `[0]` = label int64, `[1]` = probabilitas float `[N,2]`,
  `class_order = [0,1]` → **kolom 1 = p(win)**.
- Keputusan: `p1 >= 0.60` → eksekusi sinyal; di bawah threshold → skip.

## Rumus fitur (WAJIB identik dengan exporter — `src/ml/export_dataset.py`)

Semua fitur candle dihitung di **bar sinyal i** (bar yang memicu kandidat),
dengan ATR14 bar i sebagai penyebut; indikator: EMA50/ADX14/RSI14/ATR14.

```
dist_to_ema_atr   = (close - EMA50) / ATR14
adx_main          = ADX main buffer (iADX mode MAIN, period 14)
adx_pdi / adx_mdi = +DI / -DI (buffer PLUSDI_LINE / MINUSDI_LINE)
adx_di_diff       = adx_pdi - adx_mdi
rsi               = iRSI(14)
atr_normalized    = ATR14 / close * 1000.0
body_atr          = MathAbs(close - open) / ATR14
upper_shadow_atr  = (high - MathMax(open, close)) / ATR14
lower_shadow_atr  = (MathMin(open, close) - low) / ATR14
hour              = jam UTC bar open (0-23)          <-- BUKAN server time
day_of_week       = 0=Senin..6=Minggu                <-- MQ5 dt.day_of_week
                        0=Minggu; konversi: py_dow = (mq5_dow + 6) % 7
signal_type       = 1 (BUY) / 2 (SELL)
vol_ratio_20      = volume / SMA20(volume)
vol_ratio_100     = volume / SMA100(volume)
funding_rate      = funding rate HL saat sinyal (per jam, signed, raw)
```

Deteksi kandidat (threshold sumber, dari `TrendReversalStrategy`):
- BUY: `close > EMA50` && `ADX >= 15` && `+DI > -DI` && `RSI < 65` (dengan
  konfirmasi bar prev sesuai strategi), atau reversal: `RSI <= 35` + pin bar bawah.
- SELL: mirror penuh.

`funding_rate`: ambil dari HL `POST /info` → `{"type":"fundingHistory",
"coin":"BTC","startTime":now-3600000,"endTime":now}` (1 rekam terakhir) atau
metaAndAssetCtxs → `funding`. Unit = rate per jam mentah (mis. `0.0000125`),
SAMA dengan skala training (`data/BTC_funding.csv`).

## Parameter risiko (konsisten dengan label training)

| Parameter | Nilai |
|---|---|
| Entry | taker (market) di close bar sinyal |
| SL | 2.0 × ATR14 |
| TP | 1.5 × jarak SL (RR 1.5) |
| Horizon label | 30 bar (bukan parameter eksekusi; SL/TP yang mengakhiri trade) |
| Risk per trade | 1% equity (RiskLimits) |
| Kill switch | max drawdown harian sesuai `src/risk/manager.py` |

## Pemeliharaan model

1. **Retrain + re-derive threshold tiap ±3 bulan**: jalankan ulang
   `export_dataset` → `train_model` (threshold dipilih dari tabel agregat WF,
   jangan hardcode — pita edge sempit: 0,65 sudah runtuh).
2. Bandingkan distribusi probabilitas live vs training; drift besar = retraining.
3. Validasi akhir sebelum go-live: jalankan strategi lengkap di data 1H asli
   Hyperliquid (retensi ~2+ tahun di 1H) untuk cek perbedaan venue.
