# Go-Live Validation — BTC 1H di data Hyperliquid asli (Poin 1 roadmap)

Tanggal: 2026-08-30. Data: `data/BTC_1h.csv` (candleSnapshot HL, 4.993 bar 1H,
2026-02-02 s/d 2026-08-29 UTC — maksimum retensi HL).

## Hasil backtest engine lengkap (risk 1%, fee, funding, kill switch)

| Konfigurasi | Trades | WR | PF | Return | MDD |
|---|---|---|---|---|---|
| A. Tanpa filter, trailing ON | 286 | 56,3% | 0,85 | −18,88% | 19,4% |
| B. + Filter ML p≥0,60, trailing ON | 52 | 51,9% | 0,68 | −8,51% | 11,0% |
| A2. Tanpa filter, trailing OFF | 174 | 43,1% | 1,01 | **+0,6%** | 13,4% |
| B2. + Filter ML p≥0,60, trailing OFF | 41 | 31,7% | 0,59 | −12,19% | 15,2% |

## Kesimpulan: VALIDASI GAGAL — jangan go-live

1. **Edge WF (+0,075R @ p≥0,60 di data Binance 3Y) TIDAK terkonfirmasi** di
   data HL asli periode Feb–Agu 2026. Filter ML justru memilih trade yang
   LEBIH BURUK dari baseline di periode ini (B2 WR 31,7% vs A2 43,1%).
2. **Temuan samping penting**: trailing stop adalah degradasi utama.
   A2 (trailing OFF) ~breakeven (PF 1,01), sedangkan A (trailing ON) PF 0,85.
   Trailing mengubah distribusi W/L secara menguntungkan WR (56% vs 43%)
   tapi merusak PF — potongan winner jadi kecil, loser tetap penuh.
3. Konsisten dengan WF sebelumnya: fold terakhir (regime terbaru) adalah
   fold terlemah — indikasi drift/edge regime-dependent, bukan venue.

## Implikasi

- `ML_FILTER_ENABLED` default **false**; integrasi live (poin 2) tetap
  lengkap dan siap, tinggal flip config setelah model lolos validasi ulang.
- Jangan aktifkan salah satu konfigurasi di atas untuk uang riil sekarang.

## Sebelum coba validasi ulang

1. **Retrain dengan regime-aware split**: WF fold saat ini 15% test; gunakan
   window yang memastikan periode 2026 masuk train set beberapa kali, atau
   model per-regime (ATR-normalized regime classifier).
2. **Selaraskan simulasi label dengan engine**: label training memakai
   SL/TP fixed 30 bar; engine live memakai trailing. Putuskan satu:
   (a) matikan trailing di live (A2 = breakeven, dasar paling jujur), lalu
   biarkan ML memperbaikinya; atau (b) simulasi trailing di exporter agar
   label merepresentasikan eksekusi nyata.
3. **Perluas fitur regime** (realized vol 7d, basis funding, jarak dari
   ATH/ATL 90d) — hipotesis: edge ada di regime volatilitas tertentu saja.
4. Paper trading tetap boleh jalan (log-only) untuk mengumpulkan slippage
   & funding aktual, tapi tanpa eksekusi uang riil.
