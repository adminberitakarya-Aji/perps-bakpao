# Hyperliquid Perps Trading Agent

Bot trading perpetuals di Hyperliquid — port dari EA MQL5 XAUUSD ke Python,
dengan strategi pluggable, risk manager, proteksi SL/TP exchange-native, dan
infrastruktur live yang siap VPS: logging persisten, kill switch harian, dan
alert Telegram.

> ⚠️ **Realita performa:** backtest BTC 1h (Mar–Agu 2026) menunjukkan strategi
> ini **belum punya edge setelah biaya** — gross ~$0.06/trade vs fee+funding
> ~$0.054/trade, dan CI statistik semua varian trailing menyilang break-even.
> Bot benar secara teknis, tapi jangan jalankan di mainnet dengan harapan
> profit; ukur sendiri di testnet dulu.

## Status

| Komponen | Status |
|---|---|
| Strategi trend-reversal (EMA/ADX/RSI + filter trend alignment) | ✅ jalan — belum terbukti menguntungkan setelah biaya |
| Sizing risk-based (1% equity / jarak SL, cap 3× equity) | ✅ konsisten backtest & live |
| SL/TP + trailing (trigger order exchange, grouping normalTpsl) | ✅ + force-close otomatis saat proteksi gagal |
| Kill switch harian −5% (basis UTC, persist, reset otomatis) | ✅ |
| Logging persisten (`logs/bot.log`, rotasi 5MB×5) | ✅ |
| Alert Telegram (8 event, info silent / error loud) | ✅ aktif kalau token diisi |
| Smoke test live testnet penuh (entry + SL/TP pair nyata) | ⏳ menunggu faucet testnet |

Rencana pengerjaan & detail desain: lihat [roadmap.md](roadmap.md).

## Struktur

```
├── main.py                    # entry point: loop utama, wiring semua komponen
├── src/
│   ├── config.py              # baca kredensial dari .env
│   ├── client.py              # wrapper Hyperliquid SDK (Info + Exchange, SL/TP, force-close)
│   ├── engine.py              # menyatukan strategy + risk + execution + tracker harian
│   ├── data/market_data.py    # fetch candle + mid price (drop partial bar, anti-repaint)
│   ├── strategy/
│   │   ├── base.py            # interface Strategy + MarketSnapshot/SignalResult
│   │   ├── sma_crossover.py   # contoh strategi sederhana
│   │   └── trend_reversal.py  # port EA XAUUSD (EMA/ADX/RSI + pin bar/engulfing)
│   ├── risk/manager.py        # sizing risk-based, kill switch, SL/TP & trailing ATR
│   ├── execution/executor.py  # kirim order final (min notional $10, alert proteksi gagal)
│   ├── backtest/              # fetch historis + engine backtest (fee + funding)
│   └── utils/
│       ├── logger.py          # logging rotasi file + stdout
│       └── notifier.py        # alert Telegram (fire-and-forget)
├── data/                      # (gitignored) CSV historis + state posisi/harian
├── logs/                      # (gitignored) bot.log
└── tests/                     # suite script + assert (python tests/test_*.py)
```

## Setup

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# isi HL_PRIVATE_KEY dan HL_ACCOUNT_ADDRESS di .env
```

**Penting:** `HL_PRIVATE_KEY` harus private key dari **API wallet** terpisah
(dibuat via app.hyperliquid.xyz -> More -> API), bukan private key wallet
utama kamu.

## Test koneksi

```bash
python -m src.client
```

Connect ke testnet dan print mid price BTC — cara cepat cek kredensial &
koneksi sebelum menjalankan agent penuh.

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

Model biaya di engine sudah realistis:
- **Fee** taker (default 0.035% per sisi, round-trip 0.07%) — fee adalah
  biaya dominan (119× lebih besar dari funding pada window uji)
- **Funding** default `funding_rate_per_bar=0.000009` — terukur dari data
  `fundingHistory` API Hyperliquid nyata (Mar–Agu 2026), dibebankan per jam
  sesuai durasi hold (bug `bars_held` sudah diperbaiki)
- **Sizing** risk-based 1% equity per trade — konsisten dengan jalur live

**Batasan backtest ini yang perlu kamu sadari** (baca docstring di `src/backtest/engine.py` untuk detail lengkap):
- Entry price = harga close candle saat sinyal muncul, bukan harga tick presisi — hasil real bisa lebih buruk karena slippage
- Tidak ada model liquidation eksplisit — backtest ini cenderung sedikit lebih optimis dibanding kondisi leverage tinggi yang sebenarnya
- Hasil backtest yang bagus BUKAN jaminan performa live — ini alat untuk menyaring parameter yang jelas buruk, bukan bukti profitabilitas

## Testing

Semua suite memakai konvensi script + assert (tanpa framework):

```bash
python tests/test_backtest_engine.py    # sanity engine backtest (4 skenario sintetis)
python tests/test_trend_reversal.py     # perilaku strategi (7 test, aligned/unaligned)
python tests/test_daily_kill_switch.py  # kill switch: trigger/reset/persist/fallback
python tests/test_notifier.py           # notifier: silent/loud, fail-safe, wiring engine
```

## Monitoring & Alert Telegram

Isi dua variabel di `.env` untuk mengaktifkan alert (kosong = bot jalan
silent penuh):

```
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

Langkah sekali-setup:
1. Buat bot via **@BotFather** di Telegram → dapatkan token
2. Kirim pesan apa pun ke bot kamu → buka
   `https://api.telegram.org/bot<TOKEN>/getUpdates` → salin `"chat":{"id":...}`
3. Isi kedua variabel, lalu uji kirim semua template alert:
   `python -m src.utils.notifier` → cek HP

Yang akan masuk ke HP:
| Event | Tipe |
|---|---|
| 🟢 Entry (sinyal + order + SL/TP terpasang) | info, silent |
| 🔁 Trailing (SL digeser) | info, silent |
| 🏁 Posisi tertutup (SL/TP terisi / manual) | info, silent |
| 💓 Heartbeat harian (equity + PnL kemarin + posisi) | info, silent |
| 🛑 Kill switch terpicu (rugi harian ≤ −5%) | **loud** |
| 🔴 Force-close (proteksi / trailing gagal → tutup paksa) | **loud** |
| ❌ Error tak terduga di loop utama | **loud** |

Kegagalan kirim **tidak pernah** mengganggu trading (fire-and-forget,
hanya log warning). File log persisten ada di `logs/bot.log`
(rotasi 5MB × 5) — pantau dengan `tail -f logs/bot.log`.

## Menjalankan di VPS

Bot ini dirancang headless-friendly, tapi **process supervision** ada di
luar scope kode (lihat "Luar Scope" di roadmap). Instruksi minimal:

1. Clone repo + setup (sama seperti di atas) di VPS
2. Isi `.env` (pastikan file permission restriktif — berisi private key)
3. Uji dulu di testnet: `python -m src.client`, lalu biarkan `HL_USE_TESTNET=true`
4. Pilih salah satu:
   - **Linux (systemd):** buat unit `hlbot.service` dengan
     `ExecStart=/path/ke/.venv/bin/python main.py`, `Restart=always`,
     `RestartSec=30`, `WantedBy=multi-user.target`
   - **Windows:** Task Scheduler (action: run `main.py` at startup) atau NSSM
5. Alert Telegram adalah alarm utama; heartbeat harian (±07:00 WIB) adalah
   bukti bot masih hidup — kalau tidak masuk, cek VPS

## Status pengerjaan & rencana

Daftar TODO lama telah digantikan [roadmap.md](roadmap.md) — berisi audit
status per item, desain tiap fase, dan kriteria lolosnya. Fase 1–3 selesai;
Fase 4 (dokumentasi ini) adalah penutup. Tersisa: smoke test live testnet
penuh (menunggu faucet testnet terisi).
