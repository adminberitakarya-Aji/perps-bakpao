# docs/ — Blueprint Pipeline ML (Referensi)

Folder ini menyimpan **blueprint pipeline ML meta-labeling** yang berasal dari
fase riset XAUUSD, untuk direplikasi ke data perps kripto (Hyperliquid BTC/ETH)
pada Fase 2. Bukan bagian dari runtime bot — aman dihapus jika sudah tidak
diperlukan, tapi berguna sebagai spesifikasi acuan.

| File | Fungsi |
|---|---|
| `Export_ML_Dataset.mq5` | Spesifikasi ekspor fitur + labeling historis: 13 fitur ternormalisasi ATR, hanya bar kandidat sinyal, label = simulasi SL (2×ATR) / TP (RR 1.5) ke depan (horizon 30 bar) |
| `train_model.py` | Spesifikasi training: RandomForest (150 tree, depth 6, class_weight balanced), time-split 80/20, ekspor ONNX |
| `xauusd_ml_dataset.csv` | Dataset hasil ekspor versi MQL5 (28.565 baris kandidat) — dipakai sebagai fixture untuk memvalidasi bahwa Python feature-exporter menghasilkan fitur identik sebelum dipercaya pada data kripto |

## Catatan penting dari analisa dataset XAU (untuk Fase 2 kripto)

1. WR dasar kandidat ~33.7% pada RR 1.5 → rule-based saja expectancy negatif
   (~−0.16R); profitabilitas bergantung penuh pada filter ML.
2. Hubungan probabilitas model ↔ win rate terbukti monoton, tapi edge hanya
   mendekati break-even di bucket prob ~0.50 → **threshold harus dikalibrasi
   empiris per aset** (0.35 terbukti terlalu rendah).
3. Feature importance didominasi `hour` + `atr_normalized` (~42%) → fitur
   waktu wajib divalidasi ulang di kripto (24/7, efek sesi berbeda).
4. Perbaikan yang wajib saat replikasi ke kripto: label **net-of-cost**
   (taker fee + slippage + funding), threshold konsisten antara ekspor data
   dan sinyal live, dan perilaku ML error = fail-closed (tidak trade).
