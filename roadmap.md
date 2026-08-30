# Roadmap: Monitoring, Keselamatan & Kesiapan VPS

> Rencana pengerjaan turunan dari daftar TODO lama di README, setelah audit
> status 2026-08-29. Tujuan akhir: bot bisa dijalankan headless di VPS dengan
> aman (ada rem darurat), terpantau (logging + alert), dan terdokumentasi.

## Status Audit Daftar TODO Lama

| # | Item TODO lama | Status aktual |
|---|---|---|
| 1 | Equity asli dari `get_account_state()` | ✅ SELESAI — `TradingEngine._get_equity_usd()` baca `marginSummary.accountValue` |
| 2 | Tracking daily PnL untuk kill switch | ✅ SELESAI — tracker `_update_daily_pnl()` per `run_once()` (basis UTC), persist `data/daily_state.json`, reset harian, fallback-safe |
| 3 | Logging persisten | ✅ SELESAI — `src/utils/logger.py` (rotasi 5MB×5 + stdout); semua `print` jalur live diganti logger; `logs/` di gitignore |
| 4 | Monitoring / alert (Telegram) | ✅ SELESAI — `src/utils/notifier.py` (stdlib urllib, fire-and-forget, 8 event, silent/loud); wiring engine+executor+main; token via `.env` |
| 5 | Funding rate historis otomatis | 🟡 SETENGAH — default `funding_rate_per_bar=0.000009` sudah diisi dari data terukur (Mar–Agu 2026); fetch otomatis belum, prioritas rendah |

Fokus pengerjaan: **item 2, 3, 4** + rapikan dokumentasi. Item 5 (auto-fetch)
ditunda dan dicatat di bagian "Luar Scope".

---

## Urutan Pengerjaan

```
Fase 1: Logging persisten       (fondasi — fase 2 & 4 butuh ini)
Fase 2: Kill switch daily PnL    (keselamatan — paling krusial)
Fase 3: Telegram alert           (pemantauan — di atas logger)
Fase 4: Rapikan README & .env.example
```

---

## Fase 1 — Logging Persisten ✅ SELESAI (2026-08-29)

**Masalah:** `print` ke stdout hilang saat SSH session ditutup / process
crash. Di VPS kita jadi buta saat insiden.

**Desain:**
- File baru: `src/utils/logger.py`
  - stdlib `logging` saja (tanpa dependency baru)
  - `get_logger(name)` → dua handler:
    - `RotatingFileHandler("logs/bot.log", maxBytes=5MB, backupCount=5)`
    - `StreamHandler` (stdout, supaya tetap terlihat saat manual run)
  - Format: `%(asctime)s %(levelname)-7s [%(name)s] %(message)s`
- Ganti `print` → logger di jalur **live saja**: `src/engine.py`,
  `src/execution/executor.py`, `src/client.py`, `main.py`.
  Jalur backtest & tests dipertahankan `print` (tidak perlu file log).
- `logs/` ditambahkan ke `.gitignore`.

**File yang disentuh:** baru `src/utils/logger.py`; edit `src/engine.py`,
`src/execution/executor.py`, `src/client.py`, `main.py`, `.gitignore`.

**Kriteria lolos:**
1. Dry-run `run_once()` di testnet → `logs/bot.log` tercipta dan berisi
   entri ber-timestamp dengan level.
2. Tidak ada `print` tersisa di jalur live (backtest/tests boleh).
3. Semua test suite + mock engine test tetap hijau.

**Hasil validasi:** dry-run testnet menghasilkan sinyal SELL BTC live
(conf=0.65, order gagal wajar karena wallet kosong → force-close jalan);
`logs/bot.log` terisi 7 entri berlevel (INFO/WARNING/ERROR); backtest &
strategy test suite hijau; `print` tersisa hanya di jalur backtest, tests,
dan smoke-test blok `__main__` di `src/client.py`.

---

## Fase 2 — Kill Switch Daily PnL (Rem Darurat) ✅ SELESAI (2026-08-29)

**Masalah:** `RiskManager.daily_pnl_pct` tidak pernah di-update
(lihat komentar TODO di `src/risk/manager.py`). Kill switch -5%/hari ada di
kode tapi TIDAK AKAN PERNAH aktif. Untuk bot unattended ini bahaya laten:
fitur keselamatan yang mati diam-diam lebih buruk daripada tidak ada.

**Desain:**
- State harian dipersist: `data/daily_state.json` (pola sama dengan
  `live_positions.json`):
  ```json
  {"date_utc": "2026-08-30", "day_start_equity": 1000.0}
  ```
- Di `TradingEngine` (sekali per `run_once()`, bukan per symbol):
  1. Ambil equity via `_get_equity_usd()`.
  2. Jika `date_utc` di state != tanggal UTC hari ini → reset
     `day_start_equity = equity sekarang`, simpan.
  3. `daily_pnl_pct = (equity - day_start_equity) / day_start_equity`
     (guard `day_start_equity > 0`).
  4. Suntikkan: `self.risk_manager.daily_pnl_pct = daily_pnl_pct`.
  5. Jika equity fallback (wallet kosong / API gagal) → JANGAN update
     tracker (pakai nilai terakhir) supaya tidak menghitung PnL palsu.
- Hapus komentar TODO di `manager.py`; tambahkan log + (Fase 3) alert saat
  kill switch terpicu.
- Kill switch menolak ENTRY baru saja — posisi terbuka tetap dikelola
  (SL/TP/trailing tetap jalan), tidak menutup paksa posisi.

**Batasan yang disadari (dicatat, bukan di-fix sekarang):**
- Deposit/withdrawal di tengah hari terhitung sebagai "PnL" → bisa
  memicu kill switch salah. Di testnet tidak relevan. Opsi riset masa depan:
  field NNL harian dari API Hyperliquid (perlu verifikasi schema dulu).
- Zona waktu pakai **UTC** (konvensi crypto), bukan waktu server VPS.

**File yang disentuh:** edit `src/engine.py`, `src/risk/manager.py`.

**Kriteria lolos:**
1. Unit test: equity turun 6% dari awal hari → `check_and_size()` return 0
   untuk sinyal valid + state tersimpan.
2. Unit test: pergantian hari UTC → tracker reset, kill switch terbuka lagi.
3. Equity fallback → tracker tidak berubah (tidak ada PnL palsu).
4. Restart proses di tengah hari → state dimuat, kill switch konsisten.

**Hasil validasi:** suite baru `tests/test_daily_kill_switch.py` 5 test PASS
(unit blokir, trigger -6% + blokir entry via `run_once()`, restart konsisten,
rollover UTC reset baseline, fallback wallet-kosong aman + recovery -7%);
regresi backtest & strategy suite hijau; dry-run testnet menunjukkan jalur
fallback aktif tanpa PnL palsu. CATATAN: baseline `daily_state.json` baru
dibuat saat equity valid pertama kali (wallet kosong → tracker idle).

---

## Fase 3 — Telegram Alert ✅ SELESAI (2026-08-29)

**Layout disetujui:** "Indonesia, detail" — lihat method event di
`src/utils/notifier.py` untuk template final tiap event (🟢 entry /
🔁 trailing / 🏁 closed / 🛑 kill switch / 🔴 force-close ×2 / ❌ error /
💓 heartbeat harian). Info = silent (HP tidak berbunyi), kill/error =
loud. Format HTML + `<code>` monospace, waktu UTC.

**Masalah:** Bot headless di VPS tanpa cara tahu: entry apa yang terjadi,
apakah SL/TP terpasang, apakah kill switch terpicu, apakah ada error.

**Desain:**
- File baru: `src/utils/notifier.py`
  - Class `TelegramNotifier`, HTTP POST stdlib `urllib.request` ke
    `https://api.telegram.org/bot<TOKEN>/sendMessage` — tanpa dependency baru.
  - Timeout 10 detik; kegagalan kirim TIDAK BOLEH crash bot
    (swallow + log warning). Fire-and-forget.
  - Mode silent otomatis kalau `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`
    kosong di `.env` → bot tetap jalan penuh tanpa alert.
- Konfigurasi via `.env` (ditambahkan di Fase 4 ke `.env.example`):
  ```
  TELEGRAM_BOT_TOKEN=
  TELEGRAM_CHAT_ID=
  ```
- Event yang mengirim alert:
  | Event | Prioritas |
  |---|---|
  | ENTRY: sinyal + order + SL/TP terpasang | info |
  | TRAILING: SL digeser | info |
  | POSISI TERTUTUP (SL/TP terisi / manual) | info |
  | KILL SWITCH terpicu | **warning** |
  | Gagal pasang proteksi → force-close | **error** |
  | Gagal geser SL + gagal pulihkan → tutup paksa | **error** |
  | Error tak terduga di loop utama | **error** |
  | Heartbeat harian 1x/hari (equity + PnL harian) | info |
- Prasyarat pengguna (satu kali, manual): buat bot via @BotFather, ambil
  chat ID, isi `.env`.

**File yang disentuh:** baru `src/utils/notifier.py`; edit `src/config.py`
(field token + chat id), `src/engine.py` (wiring event), `.env.example`,
`src/client.py` (event force-close).

**Kriteria lolos:**
1. Token terkonfigurasi → kirim test message diterima di Telegram.
2. Tanpa token → bot jalan normal, nol error, nol kirim.
3. Simulasi kirim gagal (token salah) → bot TIDAK crash, hanya log warning.
4. Semua event di tabel terpicu minimal sekali di mock test.

**Hasil validasi:** `tests/test_notifier.py` 4 test PASS — silent mode
no-op; token palsu ke API Telegram NYATA (HTTP 404) tertelan jadi warning
tanpa crash; 8 event terpicu dengan aturan silent/loud benar + format
monospace utuh; wiring engine (entry/heartbeat/kill/closed) terpicu lewat
`run_once()` mock. Regresi 3 suite lain hijau. Dry-run testnet: silent
mode ter-log, jalur `ProtectionError` (force-close proteksi) terpicu
tanpa crash. Uji kirim NYATA tinggal isi `TELEGRAM_BOT_TOKEN` +
`TELEGRAM_CHAT_ID` lalu `python -m src.utils.notifier`.

---

## Fase 4 — Rapikan README & .env.example ✅ SELESAI (2026-08-29)

**README.md:**
- Hapus daftar "Belum diimplementasi (TODO)" yang sudah tidak akurat;
  ganti dengan tabel status singkat + link ke `roadmap.md` ini.
- Update bagian Backtest: funding default kini `0.000009`/bar (bukan 0),
  bug `bars_held` sudah diperbaiki, sizing kini risk-based 1%/trade.
- Tambah bagian "Monitoring & Alert" (env vars Telegram, cara buat bot).
- Tambah bagian singkat "Menjalankan di VPS": process supervision
  (systemd/NSSM/Task Scheduler) di luar scope kode, instruksi minimal
  ditulis.

**.env.example:**
- Tambah `TELEGRAM_BOT_TOKEN=` dan `TELEGRAM_CHAT_ID=` (kosong = off).
- Rapikan komentar (penjelasan singkat per variabel).

**Kriteria lolos:** README akurat terhadap kode (tidak ada klaim basi),
`.env.example` sinkron dengan `src/config.py`.

**Hasil:** README di-rewrite penuh — judul & deskripsi sesuai kondisi nyata,
peringatan hasil riset (belum ada edge setelah biaya) di bagian atas, tabel
status komponen, struktur direktori lengkap (backtest/utils/tests/data/logs),
bagian Testing (4 suite), Monitoring & Alert Telegram (setup + tabel event),
dan Menjalankan di VPS (systemd/NSSM minimal). Daftar "Belum diimplementasi
(TODO)" basi dihapus, digantikan link ke roadmap. `.env.example` sudah
sinkron dengan `src/config.py` (5 variabel, dengan panduan setup Telegram).

---

## Definition of Done — "VPS-Ready"

- [x] Fase 1: log file hidup, jalur live tanpa print (2026-08-29)
- [x] Fase 2: kill switch teruji (trigger + reset harian + persist) (2026-08-29)
- [x] Fase 3: alert Telegram teruji (sukses, silent, gagal-kirim aman) (2026-08-29)
- [x] Fase 4: README & .env.example akurat (2026-08-29)
- [x] Semua test suite + mock engine test hijau (2026-08-29)
- [x] Dry-run testnet `run_once()` bersih tanpa exception (2026-08-29)

## Luar Scope (dipegang untuk nanti)

1. **Process supervision VPS** (systemd unit / NSSM / Task Scheduler +
   auto-restart saat boot) — disiapkan terpisah setelah roadmap ini selesai.
2. **Funding rate auto-fetch** ke backtest — default terukur sudah dipakai;
   otomatisasi menyusul kalau ada waktu.
3. **Maker/limit entry** & selektivitas sinyal — riset edge, bukan infra.
4. Penelitian field NNL harian dari API Hyperliquid sebagai pengganti
   perhitungan equity-snapshot di Fase 2.


