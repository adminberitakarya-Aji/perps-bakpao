# Hyperliquid Trading Agent (skeleton)

Skeleton awal untuk AI trading agent perps di Hyperliquid. Strategi belum
final — struktur ini dibuat pluggable supaya gampang ganti strategi tanpa
rombak infrastruktur.

## Struktur

```
├── main.py                    # entry point, jalankan loop di sini
├── src/
│   ├── config.py               # baca kredensial dari .env
│   ├── client.py                # wrapper koneksi ke Hyperliquid SDK
│   ├── engine.py                # menyatukan strategy + risk + execution
│   ├── data/
│   │   └── market_data.py       # fetch candle & mid price
│   ├── strategy/
│   │   ├── base.py              # interface Strategy (WAJIB diimplement)
│   │   └── sma_crossover.py     # contoh strategi rule-based
│   ├── risk/
│   │   └── manager.py           # position sizing, batas rugi harian, dll
│   └── execution/
│       └── executor.py          # kirim order ke Hyperliquid
└── tests/
```

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# isi HL_PRIVATE_KEY dan HL_ACCOUNT_ADDRESS di .env
```

**Penting:** `HL_PRIVATE_KEY` harus private key dari **API wallet**
terpisah (dibuat via app.hyperliquid.xyz -> More -> API), bukan private
key wallet utama kamu.

## Test koneksi

```bash
python -m src.client
```

Ini akan connect ke testnet dan print mid price BTC — cara cepat cek
kredensial & koneksi sudah benar sebelum jalanin agent penuh.

## Jalankan agent

```bash
python main.py
```

Default jalan di **testnet** dan poll tiap 15 menit. Ganti
`HL_USE_TESTNET=false` di `.env` hanya setelah kamu yakin dengan hasil
backtest & paper trading.

## Ganti strategi

Tambahkan class baru di `src/strategy/` yang extend `Strategy` (lihat
`base.py`), lalu ganti satu baris di `main.py`:

```python
strategy = TrendReversalStrategy(require_trend_alignment=True)  # ganti ke strategi kamu
```

## Backtest

**Langkah 1 — fetch data historis** (jalankan di mesin kamu sendiri, sandbox saya tidak bisa akses API Hyperliquid langsung):

```bash
python -m src.backtest.fetch_historical --symbol BTC --interval 1h --days 180
```

Ini fetch dari **mainnet** by default (histori harga testnet lebih pendek/kurang representatif untuk backtest) — hanya baca data publik, tidak butuh dana ataupun private key asli. Hasilnya di `data/BTC_1h.csv`.

**Langkah 2 — jalankan backtest**, otomatis membandingkan `require_trend_alignment=True` vs `False`:

```bash
python -m src.backtest.run_backtest --file data/BTC_1h.csv
```

**Batasan backtest ini yang perlu kamu sadari** (baca docstring di `src/backtest/engine.py` untuk detail lengkap):
- Entry price = harga close candle saat sinyal muncul, bukan harga tick presisi — hasil real bisa lebih buruk karena slippage
- Funding rate default 0 kecuali kamu isi `funding_rate_per_bar` dengan data funding historis Hyperliquid secara manual
- Tidak ada model liquidation eksplisit — backtest ini cenderung sedikit lebih optimis dibanding kondisi leverage tinggi yang sebenarnya
- Hasil backtest yang bagus BUKAN jaminan performa live — ini alat untuk menyaring parameter yang jelas buruk, bukan bukti profitabilitas

## Belum diimplementasi (TODO)

- Ambil equity asli dari `client.get_account_state()` (saat ini hardcoded)
- Tracking daily PnL otomatis untuk kill switch di `RiskManager`
- Logging persisten (saat ini cuma print ke stdout)
- Monitoring / alert (Telegram, dsb)
- Funding rate historis otomatis (saat ini manual/default 0 di backtest)
