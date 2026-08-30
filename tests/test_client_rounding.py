"""
Test round_px: fungsi pembulatan harga sesuai aturan presisi Hyperliquid
(fix P0-3 di audit.md).

Aturan exchange: px maksimal (6 - szDecimals) desimal untuk perps
(8 - szDecimals untuk spot) DAN maksimal 5 significant figures.
`exchange.bulk_orders` SDK tidak membulatkan otomatis, jadi tanpa fungsi ini
harga SL/TP hasil round(..., 2) di risk_manager (mis. BTC 61234.56) ditolak
exchange -> ProtectionError -> force-close percuma setelah tiap entry.

Fungsi murni tanpa jaringan -- tidak butuh kredensial/koneksi.

Jalankan: python tests/test_client_rounding.py
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.client import round_px


def check(name: str, cond: bool, detail: str = ""):
    if not cond:
        raise AssertionError(f"GAGAL: {name} {detail}")
    print(f"OK: {name}")


def max_decimals_ok(px: float, allowed: int) -> bool:
    """True kalau px punya <= `allowed` desimal (di luar noise float)."""
    scaled = px * (10**allowed)
    return abs(scaled - round(scaled)) < 1e-6


# --- 1. BTC (szDecimals=5): maks 1 desimal, 5 sig-fig ---
# Kasus audit: SL hasil round(...,2) = 61234.56 (2 desimal, 7 sig-fig) -> invalid
out = round_px(61234.56, sz_decimals=5)
check("BTC 61234.56 -> 5 sig-fig", out == 61235.0, f"dapat {out}")
check("BTC hasil <= 1 desimal", max_decimals_ok(out, 1))

out = round_px(60123.44, sz_decimals=5)
check("BTC 60123.44 -> 60123.0", out == 60123.0, f"dapat {out}")
check("BTC 60123.44 hasil <= 1 desimal", max_decimals_ok(out, 1))

# --- 2. ETH-like (szDecimals=4): maks 2 desimal ---
out = round_px(3123.45678, sz_decimals=4)
check("ETH 3123.45678 -> 3123.5 (5 sig-fig)", out == 3123.5, f"dapat {out}")
check("ETH hasil <= 2 desimal", max_decimals_ok(out, 2))

# --- 3. Coin murah (szDecimals=0): maks 6 desimal ---
out = round_px(0.123456789, sz_decimals=0)
check("DOGE-like 0.123456789 -> 0.12346", out == 0.12346, f"dapat {out}")
check("DOGE-like hasil <= 6 desimal", max_decimals_ok(out, 6))

# --- 4. Konsistensi dengan _slippage_price SDK (rumus acuan) ---
# SDK: round(float(f"{px:.5g}"), (6 if not spot else 8) - sz_decimals)
sdk_ref = round(float(f"{61234.56:.5g}"), 6 - 5)
check("identik dengan rumus _slippage_price SDK", round_px(61234.56, 5) == sdk_ref)

# --- 5. Spot (asset >= 10000): 8 - szDecimals desimal ---
out = round_px(4.201234567, sz_decimals=2, is_spot=True)
# aturan 5 sig-fig tetap dominan: 4.201234567 -> 4.2012
check("spot 4.201234567 -> 4.2012 (5 sig-fig)", out == 4.2012, f"dapat {out}")
check("spot hasil <= 6 desimal", max_decimals_ok(out, 6))

# --- 6. Nilai yang sudah valid tidak berubah (idempoten) ---
check("harga sudah valid tidak berubah", round_px(60123.0, 5) == 60123.0)
check("idempoten", round_px(round_px(61234.56, 5), 5) == round_px(61234.56, 5))

print("\nSemua test round_px lulus.")
