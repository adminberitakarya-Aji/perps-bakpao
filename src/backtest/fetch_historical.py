"""
Fetch candle historis dari Hyperliquid dan simpan ke CSV untuk backtest.

Jalankan ini di mesin kamu sendiri (bukan di sandbox), karena butuh akses
langsung ke API Hyperliquid:

    python -m src.backtest.fetch_historical --symbol BTC --interval 1h --days 180

Hasilnya disimpan di data/<symbol>_<interval>.csv
"""

import argparse
import csv
import os
import time

from src.config import Config
from src.client import HyperliquidClient


def fetch_and_save(symbol: str, interval: str, days: int, use_testnet: bool = False):
    config = Config(
        private_key=os.environ.get("HL_PRIVATE_KEY", ""),
        account_address=os.environ.get("HL_ACCOUNT_ADDRESS", ""),
        use_testnet=use_testnet,
    )
    # Info endpoint (candle historis) tidak butuh signing, jadi private_key
    # boleh kosong untuk keperluan fetch data saja -- tapi HyperliquidClient
    # butuh private_key valid untuk inisialisasi wallet. Kalau cuma mau
    # fetch data tanpa trading, generate dummy key sekali pakai:
    if not config.private_key:
        import eth_account
        config.private_key = eth_account.Account.create().key.hex()

    client = HyperliquidClient(config)

    now_ms = int(time.time() * 1000)
    interval_ms = _interval_to_ms(interval)
    start_ms = now_ms - interval_ms * _bars_needed(interval, days)

    print(f"Fetching {symbol} {interval} candles, {days} hari terakhir...")

    all_candles = []
    cursor = start_ms
    # Hyperliquid membatasi jumlah candle per request, jadi fetch bertahap
    while cursor < now_ms:
        chunk_end = min(cursor + interval_ms * 5000, now_ms)
        chunk = client.get_candles(symbol, interval, cursor, chunk_end)
        if not chunk:
            break
        all_candles.extend(chunk)
        cursor = chunk_end
        time.sleep(0.2)  # sopan ke rate limit

    os.makedirs("data", exist_ok=True)
    out_path = f"data/{symbol}_{interval}.csv"
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["t", "T", "o", "h", "l", "c", "v", "n"])
        writer.writeheader()
        for c in all_candles:
            writer.writerow({k: c.get(k, "") for k in ["t", "T", "o", "h", "l", "c", "v", "n"]})

    print(f"Selesai: {len(all_candles)} candle disimpan ke {out_path}")


def _interval_to_ms(interval: str) -> int:
    unit = interval[-1]
    value = int(interval[:-1])
    multipliers = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}
    return value * multipliers[unit]


def _bars_needed(interval: str, days: int) -> int:
    interval_ms = _interval_to_ms(interval)
    return (days * 86_400_000) // interval_ms


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="BTC")
    parser.add_argument("--interval", default="1h")
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--testnet", action="store_true", help="fetch dari testnet (default: mainnet, karena histori harga testnet lebih pendek/kurang representatif)")
    args = parser.parse_args()

    fetch_and_save(args.symbol, args.interval, args.days, use_testnet=args.testnet)
