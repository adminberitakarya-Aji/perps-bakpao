"""
Fetch candle historis dari exchange eksternal (endpoint publik tanpa API
key) untuk melengkapi retensi historis Hyperliquid yang pendek:
candleSnapshot HL cuma menyimpan ~5000 bar terakhir -- di 15m itu ~52 hari.
Untuk deep-history 6-12 bulan di 15m, data itu tidak cukup.

    python -m src.backtest.fetch_external --symbol BTCUSDT --interval 15m --days 270

Sumber dicoba berurutan sampai berhasil (network tertentu memblokir
sebagian domain):
  1. Binance  (api.binance.com, klines, 1000 bar/request)
  2. OKX      (www.okx.com, history-candles, 100 bar/request)
  3. Gate.io  (api.gateio.ws, futures candlesticks, 1000 bar/request)

Schema CSV sama persis dengan fetch_historical.py (t,T,o,h,l,c,v,n) supaya
load_candles() di run_backtest.py bisa dipakai tanpa perubahan.

CATATAN VALIDITAS: harga BTC antar-venue beda hanya beberapa bps, jadi
statistik sinyal teknikal praktis identik. Fee, funding, dan slippage
tetap pakai angka Hyperliquid saat backtest. Verifikasi ulang strategi di
data 15m asli Hyperliquid (maks ~52 hari) sebelum go-live.
"""

import argparse
import csv
import os
import time

import requests

TIMEOUT = 20
SLEEP = 0.15

_UNIT_MIN = {"m": 1, "h": 60, "d": 1440}


def _interval_minutes(interval: str) -> int:
    return int(interval[:-1]) * _UNIT_MIN[interval[-1]]


def fetch_binance(symbol, interval, start_ms, end_ms):
    """BTCUSDT; klines paginasi maju pakai startTime; 1000 bar/request."""
    interval_ms = _interval_minutes(interval) * 60_000
    out = {}
    cursor = start_ms
    while cursor < end_ms:
        r = requests.get("https://api.binance.com/api/v3/klines", params={
            "symbol": symbol, "interval": interval,
            "startTime": cursor, "endTime": end_ms, "limit": 1000,
        }, timeout=TIMEOUT)
        r.raise_for_status()
        rows = r.json()
        if not rows:
            break
        for row in rows:
            t = int(row[0])
            out[t] = {"t": t, "T": t + interval_ms - 1,
                      "o": row[1], "h": row[2], "l": row[3], "c": row[4],
                      "v": row[5], "n": row[8]}
        last_t = int(rows[-1][0])
        if last_t <= cursor - interval_ms:
            break
        cursor = last_t + interval_ms
        time.sleep(SLEEP)
    return [out[t] for t in sorted(out)]


def fetch_okx(symbol, interval, start_ms, end_ms):
    """BTC-USDT; history-candles paginasi mundur pakai after; 100 bar/request."""
    interval_ms = _interval_minutes(interval) * 60_000
    # OKX format bar: 15m / 1H / 1D
    suffix = {"m": "m", "h": "H", "d": "D"}[interval[-1]]
    bar = interval[:-1] + suffix
    out = {}
    after = end_ms
    while after > start_ms:
        r = requests.get("https://www.okx.com/api/v5/market/history-candles", params={
            "instId": symbol, "bar": bar, "after": after, "limit": 100,
        }, timeout=TIMEOUT)
        r.raise_for_status()
        payload = r.json()
        if payload.get("code") != "0":
            raise RuntimeError(f"OKX error: {payload.get('msg')}")
        rows = payload.get("data", [])
        if not rows:
            break
        # row = [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
        oldest = min(int(row[0]) for row in rows)
        for row in rows:
            t = int(row[0])
            if t < start_ms:
                continue
            out[t] = {"t": t, "T": t + interval_ms - 1,
                      "o": row[1], "h": row[2], "l": row[3], "c": row[4],
                      "v": row[5], "n": ""}
        if oldest >= after:
            break
        after = oldest - 1
        time.sleep(SLEEP)
    return [out[t] for t in sorted(out)]


def fetch_binance_vision(symbol_base, interval, start_ms, end_ms):
    """Arsip bulanan/harian resmi Binance di S3 (data.binance.vision) -- sering
    bisa diakses walau api.binance.com diblokir. Retensi: seluruh histori.
    Timestamp open_time dalam MIKROdetik; baris header baru ada di file baru."""
    import io
    import zipfile

    interval_ms = _interval_minutes(interval) * 60_000
    out = {}

    def _parse_zip(content):
        zf = zipfile.ZipFile(io.BytesIO(content))
        rows = zf.read(zf.namelist()[0]).decode().strip().split("\n")
        for line in rows:
            parts = line.split(",")
            if not parts[0].strip().isdigit():  # skip header (kalau ada)
                continue
            # File arip lama (<2025) pakai open_time MILLISECOND, file baru
            # MIKROdetik. Deteksi dari magnitude (ms ~1.7e12, us ~1.7e15).
            ts = int(parts[0])
            t = ts // 1000 if ts > 10**14 else ts
            if t < start_ms or t > end_ms:
                continue
            out[t] = {"t": t, "T": t + interval_ms - 1,
                      "o": parts[1], "h": parts[2], "l": parts[3], "c": parts[4],
                      "v": parts[5], "n": parts[8] if len(parts) > 8 else ""}

    # 1) zip bulanan utk semua bulan lengkap
    m_start = time.gmtime(start_ms / 1000)
    y, m = m_start.tm_year, m_start.tm_mon
    now = time.gmtime()
    while (y, m) <= (now.tm_year, now.tm_mon):
        url = (f"https://data.binance.vision/data/spot/monthly/klines/"
               f"{symbol_base}/{interval}/{symbol_base}-{interval}-{y:04d}-{m:02d}.zip")
        r = requests.get(url, timeout=60)
        if r.status_code == 200:
            _parse_zip(r.content)
        elif r.status_code != 404:
            r.raise_for_status()
        m += 1
        if m > 12:
            y, m = y + 1, 1
        time.sleep(SLEEP)

    # 2) sisa bulan berjalan pakai zip harian (zip bulanan cuma utk bulan lengkap)
    import calendar
    day_ms = 86_400_000
    month_start_ms = calendar.timegm(
        time.strptime(time.strftime("%Y-%m-01", time.gmtime(end_ms / 1000)), "%Y-%m-%d")
    ) * 1000
    day = month_start_ms
    while day <= end_ms:
        gt = time.gmtime(day / 1000)
        url = (f"https://data.binance.vision/data/spot/daily/klines/"
               f"{symbol_base}/{interval}/{symbol_base}-{interval}-"
               f"{gt.tm_year:04d}-{gt.tm_mon:02d}-{gt.tm_mday:02d}.zip")
        try:
            r = requests.get(url, timeout=30)
            if r.status_code == 200:
                _parse_zip(r.content)
            elif r.status_code != 404:
                r.raise_for_status()
        except requests.exceptions.RequestException:
            pass  # satu hari gagal tidak fatal
        day += day_ms
        time.sleep(SLEEP)

    return [out[t] for t in sorted(out)]


def fetch_gate(symbol, interval, start_ms, end_ms):
    """BTC_USDT (futures USDT); candlesticks paginasi maju; maks 1000 interval
    per request (API menolak rentang lebih panjang dengan HTTP 400)."""
    interval_ms = _interval_minutes(interval) * 60_000
    out = {}
    cursor = start_ms
    while cursor < end_ms:
        chunk_end = min(cursor + 999 * interval_ms, end_ms)
        r = requests.get("https://api.gateio.ws/api/v4/futures/usdt/candlesticks", params={
            "contract": symbol, "interval": interval,
            "from": cursor // 1000, "to": chunk_end // 1000,
            # NOTE: Gate menolak `limit` + `from` + `to` bersamaan (HTTP 400),
            # jadi limit sengaja tidak dikirim -- rentang 999 interval otomatis
            # menghasilkan <= 1000 bar.
        }, timeout=TIMEOUT)
        if r.status_code == 400:
            # "Candlestick too long ago. Maximum 10000 points recently" ->
            # window di luar retensi; maju ke chunk berikutnya
            cursor = chunk_end + interval_ms
            continue
        r.raise_for_status()
        rows = r.json()
        if not rows:
            # window ini kosong (di luar retensi?) -- maju per 999 interval
            cursor = chunk_end + interval_ms
            continue
        for row in rows:
            t = int(row["t"]) * 1000
            out[t] = {"t": t, "T": t + interval_ms - 1,
                      "o": row["o"], "h": row["h"], "l": row["l"], "c": row["c"],
                      "v": row.get("v", ""), "n": ""}
        last_t = int(rows[-1]["t"]) * 1000
        if last_t <= cursor - interval_ms:
            break
        cursor = last_t + interval_ms
        time.sleep(SLEEP)
    return [out[t] for t in sorted(out)]


def fetch_multi_source(symbol_base, interval, days):
    """Coba tiap sumber sampai ada yang berhasil. symbol_base format Binance
    (BTCUSDT); dipetakan otomatis ke format OKX/Gate."""
    start_ms = int(time.time() * 1000) - days * 86_400_000
    end_ms = int(time.time() * 1000)
    base = symbol_base[:-4]  # "BTCUSDT" -> "BTC"
    sources = [
        ("Binance Vision (arsip S3)", fetch_binance_vision, symbol_base),
        ("Binance API", fetch_binance, symbol_base),
        ("OKX", fetch_okx, f"{base}-USDT"),
        ("Gate.io (retensi ~10000 bar)", fetch_gate, f"{base}_USDT"),
    ]
    for name, fn, sym in sources:
        try:
            print(f"Mencoba {name} ({sym})...")
            candles = fn(sym, interval, start_ms, end_ms)
            if candles:
                print(f"OK: {len(candles)} candle dari {name}")
                return candles, name
            print(f"{name}: 0 candle")
        except Exception as e:
            print(f"{name} gagal: {type(e).__name__}: {e}")
    return [], None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--interval", default="15m")
    parser.add_argument("--days", type=int, default=270)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    candles, source = fetch_multi_source(args.symbol, args.interval, args.days)
    if not candles:
        raise SystemExit("Semua sumber eksternal gagal -- periksa koneksi/blokir domain")

    os.makedirs("data", exist_ok=True)
    out_path = args.out or f"data/{args.symbol}_{args.interval}_ext.csv"
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["t", "T", "o", "h", "l", "c", "v", "n"])
        writer.writeheader()
        writer.writerows(candles)

    first = time.strftime("%Y-%m-%d", time.gmtime(candles[0]["t"] / 1000))
    last = time.strftime("%Y-%m-%d", time.gmtime(candles[-1]["t"] / 1000))
    print(f"Selesai (sumber {source}): {len(candles)} candle "
          f"({first} s/d {last} UTC) -> {out_path}")


if __name__ == "__main__":
    main()
