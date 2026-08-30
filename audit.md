# 🔍 Laporan Audit Mendalam — Hyperliquid Perps Trading Agent

> Tanggal audit: 2026-08-30
> Scope: seluruh kode inti (`main.py`, `src/` — engine, client, executor, risk manager, strategi, data, ML inference, notifier, logger), pipeline backtest & ML (`src/backtest/`, `src/ml/`), dokumentasi (`README.md`, `roadmap.md`, `docs/`), keamanan (`.env`, riwayat git, `.gitignore`), dependensi (`requirements.txt`, isi `.venv`), dan test suite (`tests/`).
> Metode: review statis seluruh file, verifikasi aturan exchange terhadap source SDK Hyperliquid yang terpasang di `.venv`, pemeriksaan riwayat git, dan eksekusi seluruh test suite.

---

## Ringkasan Eksekutif

| Aspek | Penilaian | Catatan |
|---|---|---|
| Arsitektur & pemisahan tanggung jawab | ✅ Baik | strategy/risk/execution/data terpisah rapi, satu sumber kebenaran logika sinyal |
| Dokumentasi | ✅ Baik (1 basi) | Jujur soal edge yang belum terbukti; 1 klaim README tidak cocok kode |
| Test suite | ✅ Lulus semua | 4/4 suite lulus; tapi jalur kritis bermasalah tidak tercakup test |
| Keamanan secret | ✅ Baik | Private key tidak pernah masuk git; API wallet terpisah |
| Filosofi fail-closed | ⚠️ Tidak konsisten | Dua pelanggaran: default ML filter ON, fallback sizing $1000 |
| Kesiapan live | ❌ Belum siap | 3 bug kritis akan membuat bot crash / force-close tiap entry |

**Kesimpulan utama:** kode secara arsitektur bagus dan dokumentasinya jujur, tetapi ada **3 bug kritis (P0)** yang akan membuat bot (a) crash saat fresh install, dan (b) force-close setiap entry BTC karena harga SL/TP ditolak exchange. Tidak ada dari temuan ini yang tertangkap test suite saat ini.

---

## 🔴 Temuan KRITIS (P0) — wajib diperbaiki sebelum jalankan bot

### P0-1 · `main.py` pasti crash: `onnxruntime` & `numpy` tidak ada di `requirements.txt`

- **Lokasi:** `main.py:9` → `from src.ml.inference import MLSignalFilter` (import tanpa syarat); `src/ml/inference.py:18-19` → `import numpy as np`, `import onnxruntime as ort` (level modul).
- **Bukti:** `requirements.txt` hanya berisi `hyperliquid-python-sdk, eth-account, python-dotenv, ta, pandas`. Verifikasi langsung di `.venv` proyek: `import onnxruntime` → **MISSING**, `import numpy` → **MISSING**.
- **Dampak:** setup baru di VPS mengikuti README (`pip install -r requirements.txt` → `python main.py`) akan gagal dengan `ImportError` sebelum bot berjalan sama sekali.
- **Perbaikan yang disarankan:**
  1. Tambahkan `onnxruntime` dan `numpy` ke `requirements.txt` (dengan version pin), **atau**
  2. Jadikan import ML lazy/opsional di `main.py` (hanya import `MLSignalFilter` jika `ml_filter_enabled`), sehingga user yang tidak pakai ML tidak perlu dependency berat.

### P0-2 · Default `ML_FILTER_ENABLED` kebalikan dari dokumen keamanan (fail-closed dilanggar)

- **Lokasi:** `src/config.py:45` → `ml_enabled = os.environ.get("ML_FILTER_ENABLED", "true")` → **default `true`**.
- **Kontradiksi dengan:**
  - `src/config.py:28` — default dataclass `ml_filter_enabled: bool = False`, dengan komentar eksplisit "DEFAULT FALSE: validasi venue ... TIDAK mengkonfirmasi edge WF".
  - `docs/go_live_validation.md:29` — "`ML_FILTER_ENABLED` default **false**".
  - `README.md` & `main.py:18-20` — "jangan aktifkan ml_filter sebelum model tervalidasi ulang di data HL".
- **Dampak:** `.env` aktual TIDAK memuat `ML_FILTER_ENABLED`, jadi bot berjalan dengan **filter ML AKTIF** memakai model yang terdokumentasi **belum lolos validasi** (validasi venue GAGAL — filter justru memilih trade lebih buruk di data HL Feb–Agu 2026). Ini kebalikan prinsip fail-closed yang ditulis di mana-mana.
- **Temuan tambahan:** `.env.example` tidak mencantumkan `ML_FILTER_ENABLED`, `ML_THRESHOLD`, `ML_MODEL_PATH` sama sekali — user tidak tahu variabel ini ada.
- **Perbaikan:** ganti default di `config.py:45` menjadi `"false"`, dan tambahkan ketiga variabel ML ke `.env.example` dengan komentar peringatan.

### P0-3 · Harga SL/TP BTC hampir pasti DITOLAK exchange → force-close setelah setiap entry

- **Lokasi:**
  - `src/risk/manager.py:99` → `return round(sl, 2), round(tp, 2)` (harga dengan **2 desimal**, mis. `61234.56`).
  - `src/client.py:109-138` → `place_tpsl_pair` mengirim harga mentah ke `exchange.bulk_orders(...)`.
- **Bukti (verifikasi SDK terpasang):** di `.venv/Lib/site-packages/hyperliquid/exchange.py:131-132`, pembulatan harga hanya terjadi di `_slippage_price()` (dipakai `market_open`/`market_close`). **`bulk_orders()` TIDAK melakukan rounding apa pun** — harga dikirim apa adanya.
- **Aturan Hyperliquid untuk perps:** `px` maksimal **(6 − szDecimals) desimal** dan maksimal 5 significant figures. BTC `szDecimals = 5` → hanya boleh **1 desimal**. Contoh `61234.56` = 2 desimal & 7 sig-fig → melanggar dua aturan sekaligus → order trigger ditolak.
- **Dampak (rantai kegagalan):** entry market terisi → pasang SL/TP ditolak exchange → `place_tpsl_pair` raise → `ProtectionError` → posisi langsung di-tutup-paksa (`client.py:90-97`). Hasilnya: **setiap entry buruk biaya fee bolak-balik tanpa pernah benar-benar trading**. Path trailing di `engine.py:325-327` juga memakai `place_tpsl_pair` → kena masalah yang sama.
- **Perbaikan:** buat helper rounding harga yang mengambil `szDecimals` dari `self.info.asset_to_sz_decimals` dan menerapkan aturan HL (maks `6 − szDecimals` desimal + 5 sig-fig, pola sama seperti `_slippage_price` SDK), lalu terapkan di `place_tpsl_pair` (dan di mana pun harga px dikirim manual).
- **Catatan verifikasi:** temuan ini berbasis review statis SDK + dokumentasi aturan harga HL; konfirmasi penuh butuh smoke test live testnet (yang memang masih pending di README). Tapi secara statis buktinya kuat dan konsisten.

---

## 🟠 Temuan TINGGI (P1) — risiko nyata di jalur live

### P1-4 · Tidak ada self-healing saat SL trigger hilang

- **Lokasi:** `src/engine.py:309-313`.
- **Perilaku:** jika SL trigger tidak ditemukan di exchange (`sl_active is None`), bot hanya `log.error("... PERIKSA MANUAL ...")` lalu `return`. Tidak ada upaya memasang ulang SL dari state, dan tidak ada force-close.
- **Dampak:** posisi telanjang (tanpa stop-loss) dibiarkan terbuka tanpa batas waktu — hanya di-log ulang tiap jam. Ini bertentangan dengan filsafat yang ditulis di `client.py`: *"engine ini tidak pernah meninggalkan posisi telanjang tanpa stop-loss"*.
- **Diperparah oleh:** pola trailing cancel-then-replace (`engine.py:325-327`): ada window (plus crash window jika proses mati di antara cancel dan replace) tanpa SL; pada siklus berikutnya kondisi ini jatuh ke jalur P1-4 di atas dan **tidak pernah dipulihkan**.
- **Perbaikan:** saat `sl_active is None`: (1) re-place SL (+TP) dari state yang tersimpan; (2) kalau re-place gagal → `market_close` + alert loud. Pertimbangkan juga `exchange.modify_order` daripada cancel-then-replace.

### P1-5 · Respons order tidak pernah divalidasi (state posisi fantasi)

- **Lokasi:** `src/client.py:57-98` (`place_market_order`) dan `src/execution/executor.py:42-45`.
- **Perilaku:** SDK `market_open`/`bulk_orders` **tidak me-raise exception** saat order ditolak — mereka mengembalikan dict, mis. `{'status': 'err', 'response': '...'}`. Tidak ada kode yang mengecek `result["status"]`.
- **Dampak:** di `engine.py:235-243`, state posisi dicatat (`live_positions[symbol] = {...}`) untuk hasil `exec_result` yang bukan `None` — termasuk respons error. Akibatnya SL/TP/trailing "dikelola" untuk posisi yang sebenarnya tidak pernah ada (state fantasi), dan notifikasi entry terkirim untuk order gagal.
- **Perbaikan:** validasi `result["status"] == "ok"` (dan isi `response`) sebelum menganggap order sukses; raise exception / return None jika tidak.

### P1-6 · Fallback sizing `$1000` tidak fail-closed — risiko oversizing

- **Lokasi:** `src/engine.py:347-357` (`_get_equity_usd`).
- **Perilaku:** jika `get_account_state` gagal/kosong, sizing memakai equity fiktif **1000.0** dengan hanya log warning.
- **Dampak:** jika equity asli jauh lebih kecil (mis. $100), notional dihitung dari $1000 → posisi oversize relatif terhadap dana nyata → risiko liquidation meningkat. Tracker PnL harian sudah benar memakai `_get_equity_or_none()` yang fail-closed (komennya sendiri bilang "nilai fallback TIDAK boleh dipakai di sini") — tetapi sizing justru melanggar prinsip yang sama.
- **Perbaikan:** skip entry (fail-closed) ketika equity tidak tersedia, konsisten dengan tracker harian.

### P1-7 · Timing entry tidak selaras candle & backtest

- **Lokasi:** `main.py:13,64-71` (`POLL_INTERVAL_SECONDS = 3600`, `time.sleep(3600)` dari waktu start proses).
- **Perilaku:** polling tidak align ke boundary candle 1H. Sinyal dievaluasi dari candle yang sudah close (bagus, anti-repaint di `market_data.py`), tetapi entry dieksekusi di **mid price saat poll** — yang bisa sampai ~1 jam setelah candle sinyal close.
- **Dampak:** mismatch dengan backtest, yang mengasumsikan entry di close candle sinyal (`backtest/engine.py` docstring). Hasil backtest menjadi sistematis optimis vs live; drift poll juga menumpuk karena `sleep(3600)` tidak mengoreksi waktu eksekusi `run_once` itu sendiri.
- **Perbaikan:** ganti sleep tetap dengan sleep-until: tidur sampai `menit :05` jam berikutnya (buffer setelah close candle), sehingga evaluasi selalu tepat setelah candle baru close.

---

## 🟡 Temuan SEDANG (P2)

| # | Temuan | Lokasi | Dampak / Catatan |
|---|---|---|---|
| P2-8 | README bilang "poll tiap 15 menit", kode `POLL_INTERVAL_SECONDS = 3600` (1 jam) | `README.md:~83` vs `main.py:13` | Dokumen basi — perbarui README |
| P2-9 | Trailing memakai pola **cancel-then-replace** — ada window tanpa SL; SDK menyediakan `modify_order` yang lebih aman | `engine.py:325-327` | Lihat P1-4 (crash window membuat kondisi permanen) |
| P2-10 | Kill switch dicek hanya 1× per jam (per `run_once`) | `engine.py:182` | Gap risk antar-poll: drawdown intrajam bisa jauh melewati −5% sebelum terdeteksi |
| P2-11 | Heartbeat selalu kirim `kill_triggered=False` hardcoded | `engine.py:147` | Seharusnya kirim nilai `self.daily_state["kill_triggered"]` aktual |
| P2-12 | `requirements.txt` tanpa version pin sama sekali | `requirements.txt` | Build tidak reproducible; `ta` dkk. rawan breaking change |
| P2-13 | `.gitignore` tidak mencakup `models/`, `rv2_hl.txt` (427KB), `wf_i2_*.txt`, `docs/xauusd_ml_dataset.csv` (3.7MB) — semuanya untracked | `.gitignore` | Rawan ke-commit tidak sengaja; putuskan per file: commit atau ignore |
| P2-14 | ~9 file modified + ~15 file untracked belum di-commit | `git status` | Kerjaan Fase 2–4 rawan hilang; commit secepatnya |
| P2-15 | Indikator dihitung ulang 3× per siklus per simbol | `engine.py:359-392` | Inefficiency murni; hitung sekali dan bagikan hasilnya |
| P2-16 | Rekonstruksi state posisi tanpa `entry_atr` menonaktifkan trailing permanen, tanpa alert | `engine.py:291-301` | Perilaku terdokumentasi, tapi tambahkan notifikasi ke user |
| P2-17 | `market_data.py` membandingkan waktu candle dengan **jam lokal** — clock skew bisa menyertakan candle yang belum close (repaint) | `market_data.py:23-31` | Minor; pertimbangkan margin toleransi |

---

## 🟢 Keamanan — KONDISI BAIK ✅

| Pemeriksaan | Hasil |
|---|---|
| `.env` gitignored & tidak pernah masuk riwayat git (`git log --all -- .env` = kosong) | ✅ |
| Tidak ada secret ter-hardcode di source code | ✅ |
| Private key = API wallet terpisah (66 char, format `0x…` benar), bukan key wallet utama | ✅ |
| Notifier fire-and-forget + `html.escape` — kegagalan kirim tidak pernah mengganggu trading | ✅ |
| Force-close otomatis saat proteksi gagal (`client.py:90-97`) | ✅ (desain benar; efektivitas terganggu P0-3) |

**Catatan:** repo punya remote GitHub (`origin: .../perps-bakpao.git`). Pastikan remote **private** sebelum push, dan rotasi API wallet key jika pernah ter-copy ke mesin lain / tampil di layar bersama.

---

## 🧪 Testing

**Hasil eksekusi** (konvensi project = script + assert, dijalankan via `.venv`):

| Suite | Hasil |
|---|---|
| `tests/test_backtest_engine.py` | ✅ LULUS |
| `tests/test_daily_kill_switch.py` | ✅ LULUS |
| `tests/test_notifier.py` | ✅ LULUS |
| `tests/test_trend_reversal.py` | ✅ LULUS |

**Cakupan yang hilang** — justru jalur-jalur bermasalah di atas tidak tercakup:

1. Rounding/validasi harga SL/TP terhadap aturan Hyperliquid (akan menangkap P0-3).
2. Validasi respons order `status: "err"` (akan menangkap P1-5).
3. Self-healing saat SL trigger hilang (akan menangkap P1-4).
4. Sizing saat equity tidak tersedia (akan menangkap P1-6).
5. Test `client`/`engine` dengan mock exchange — jalur eksekusi live belum tersentuh test.

**Catatan infra:** `pytest` tidak terinstall di venv (ada `.pytest_cache` — indikasi pernah dipakai di lingkungan lain). Tidak ada CI yang menjalankan test otomatis.

---

## 📊 Penilaian Pipeline ML

**Kekuatan:**
- Satu sumber kebenaran fitur: exporter (`src/ml/export_dataset.py`) dan inference (`src/ml/inference.py`) memakai rumus fitur identik, dan keduanya memanggil `_to_df`/`_compute_indicators`/`_decide_from_rows` strategi yang sama dengan live.
- Fail-closed di inferensi: error apa pun → sinyal DITOLAK; fitur NaN → ditolak; window < 520 bar → ditolak.
- Label net-of-cost (fee + slippage + funding prorata durasi) — lebih jujur dari label gross.
- Simulasi trailing di exporter eksplisit tidak look-ahead (SL baru efektif untuk bar berikutnya).
- Kandidat sinyal dataset = persis konfigurasi produksi (`require_trend_alignment=False` di kedua sisi, ditegaskan di `main.py:27-32`).

**Kelemahan (sebagian besar sudah diakui sendiri di `docs/go_live_validation.md`):**
- Validasi venue di data HL asli (Feb–Agu 2026) **GAGAL**: filter ML memilih trade lebih buruk dari baseline (B2 WR 31,7% vs A2 43,1%); PF semua konfigurasi trailing ≤ 0,85.
- Mismatch label vs eksekusi: label training SL/TP fixed + horizon 30 bar; engine live memakai trailing tanpa horizon — terdokumentasi sebagai item "sebelum validasi ulang".
- Temuan samping penting: **trailing stop adalah degradasi utama** (A2 trailing OFF ~breakeven PF 1,01 vs A trailing ON PF 0,85) — pertimbangkan opsi (a) di docs: matikan trailing di live.

**Kesimpulan ML:** keputusan "default nonaktif + jangan go-live" adalah keputusan yang benar — sayangnya bug P0-2 membuat keputusan itu tidak efektif di config parsing.

---

## 🗺️ Prioritas Perbaikan (urutan eksekusi yang disarankan)

| Prioritas | Item | Effort |
|---|---|---|
| 1 | **P0-1** — tambah `onnxruntime` + `numpy` ke `requirements.txt` (atau lazy-import ML di `main.py`) | Kecil |
| 2 | **P0-2** — default `ML_FILTER_ENABLED` → `"false"` di `config.py:45` + lengkapi `.env.example` | Kecil |
| 3 | **P0-3** — helper rounding harga HL (`6 − szDecimals` desimal, 5 sig-fig) di `place_tpsl_pair` | Sedang |
| 4 | **P1-5** — validasi `result["status"]` sebelum catat state posisi | Kecil |
| 5 | **P1-6** — hapus fallback $1000; skip entry jika equity tidak tersedia | Kecil |
| 6 | **P1-4** — self-healing SL hilang (re-place → force-close → alert) | Sedang |
| 7 | **P1-7** — polling align ke boundary candle | Kecil |
| 8 | **P2-8/12/14** — perbarui README, pin dependencies, commit semua pekerjaan | Kecil |
| 9 | Tambah unit test untuk item 3–6 (bisa tanpa jaringan via mock) | Sedang |
| 10 | Smoke test live testnet penuh (sudah direncanakan di README) | — |

---

*Metode audit: review statis seluruh file inti + verifikasi source SDK Hyperliquid terpasang di `.venv` + pemeriksaan riwayat git + eksekusi test suite. Tidak ada order nyata dikirim saat audit. Temuan P0-3 (presisi harga) perlu konfirmasi final lewat smoke test testnet, meskipun bukti statisnya kuat dan konsisten.*