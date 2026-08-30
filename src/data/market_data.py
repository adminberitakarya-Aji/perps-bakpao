"""
Fetch candle & mid price dari Hyperliquid untuk jalur live.

Anti-repaint: candle TERAKHIR dari API bisa jadi candle yang masih berjalan
(belum close) -- kalau itu ikut dipakai sebagai "bar terkonfirmasi" oleh
strategi, sinyal bisa berubah-ubah tiap kali di-fetch ulang dalam candle
yang sama ("repainting"). Candle yang masih berjalan di-drop di sini supaya
strategi selalu menerima candle yang benar-benar sudah close.
"""

import time

from src.client import HyperliquidClient
from src.strategy.base import MarketSnapshot

# toleransi clock skew lokal vs server exchange (anti-repaint, P2-17)
CLOCK_SKEW_GUARD_MS = 5_000


def fetch_snapshot(
    client: HyperliquidClient,
    symbol: str,
    interval: str = "1h",
    lookback_candles: int = 60,
) -> MarketSnapshot:
    now_ms = int(time.time() * 1000)
    interval_ms = _interval_to_ms(interval)
    start_ms = now_ms - (interval_ms * lookback_candles)

    candles = client.get_candles(symbol, interval, start_ms, now_ms)

    # drop candle yang masih berjalan (close time "T" di masa depan / >= now).
    # Margin skew: jam lokal bisa selisih beberapa detik dari server HL --
    # kalau jam lokal LEBIH CEPAT, candle yang masih berjalan bisa terlihat
    # sudah close (T <= now) dan ikut dipakai -> repaint. Karena poll sudah
    # +5 menit setelah close candle, menahan candle ekstra 5 detik tidak
    # menunda apa pun (fix P2-17).
    if candles and int(candles[-1].get("T", 0)) > now_ms - CLOCK_SKEW_GUARD_MS:
        candles = candles[:-1]

    mid_price = client.get_mid_price(symbol)

    return MarketSnapshot(symbol=symbol, mid_price=mid_price, candles=candles)


def _interval_to_ms(interval: str) -> int:
    unit = interval[-1]
    value = int(interval[:-1])
    multipliers = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}
    return value * multipliers[unit]
