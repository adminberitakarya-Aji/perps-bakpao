import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import random
from src.strategy.trend_reversal import TrendReversalStrategy
from src.strategy.base import MarketSnapshot, Signal


def make_trending_candles(n=80, start_price=100.0, drift=0.5, noise=0.6, seed=1):
    """Uptrend realistis dengan pullback berkala (bukan garis lurus),
    supaya RSI tidak nyangkut permanen di 100."""
    random.seed(seed)
    candles = []
    price = start_price
    for i in range(n):
        # tiap ~8 candle, beri pullback kecil supaya RSI bisa "bernapas"
        local_drift = -drift * 1.5 if i % 8 == 7 else drift
        o = price
        price += local_drift + random.uniform(-noise, noise)
        c = price
        h = max(o, c) + random.uniform(0, noise)
        l = min(o, c) - random.uniform(0, noise)
        candles.append({"t": i, "T": i + 1, "o": o, "h": h, "l": l, "c": c, "v": 100})
    return candles


def make_downtrending_candles(n=80, start_price=100.0, drift=-0.5, noise=0.6, seed=2):
    random.seed(seed)
    candles = []
    price = start_price
    for i in range(n):
        local_drift = -drift * 1.5 if i % 8 == 7 else drift
        o = price
        price += local_drift + random.uniform(-noise, noise)
        c = price
        h = max(o, c) + random.uniform(0, noise)
        l = min(o, c) - random.uniform(0, noise)
        candles.append({"t": i, "T": i + 1, "o": o, "h": h, "l": l, "c": c, "v": 100})
    return candles


def make_flat_candles(n=80, price=100.0, noise=0.1, seed=3):
    random.seed(seed)
    candles = []
    for i in range(n):
        o = price + random.uniform(-noise, noise)
        c = price + random.uniform(-noise, noise)
        h = max(o, c) + random.uniform(0, noise / 2)
        l = min(o, c) - random.uniform(0, noise / 2)
        candles.append({"t": i, "T": i + 1, "o": o, "h": h, "l": l, "c": c, "v": 100})
    return candles


def run_test(name, candles, expected_signals):
    strategy = TrendReversalStrategy()
    snapshot = MarketSnapshot(symbol="TEST", mid_price=candles[-1]["c"], candles=candles)
    result = strategy.generate_signal(snapshot)
    status = "OK" if result.signal in expected_signals else "UNEXPECTED"
    print(f"[{status}] {name}: signal={result.signal.value} conf={result.confidence} | {result.reason}")
    return result


def run_rolling_test(name, candles, forbidden_signal, strategy, min_bars=52):
    """Jalankan strategi di tiap bar (rolling window) sepanjang data,
    hitung distribusi sinyal."""
    counts = {Signal.BUY: 0, Signal.SELL: 0, Signal.HOLD: 0}
    for end in range(min_bars, len(candles) + 1):
        window = candles[:end]
        snapshot = MarketSnapshot(symbol="TEST", mid_price=window[-1]["c"], candles=window)
        result = strategy.generate_signal(snapshot)
        counts[result.signal] += 1

    forbidden_count = counts[forbidden_signal] if forbidden_signal else 0
    label = "" if forbidden_signal is None else f" | sinyal '{forbidden_signal.value}'={forbidden_count}"
    print(f"{name}: BUY={counts[Signal.BUY]} SELL={counts[Signal.SELL]} HOLD={counts[Signal.HOLD]}{label}")
    return counts


if __name__ == "__main__":
    print("=== Test 1: Uptrend kuat -> ekspektasi BUY atau HOLD (jangan SELL) ===")
    run_test("uptrend", make_trending_candles(), expected_signals=[Signal.BUY, Signal.HOLD])

    print("\n=== Test 2: Downtrend kuat -> ekspektasi SELL atau HOLD (jangan BUY) ===")
    run_test("downtrend", make_downtrending_candles(), expected_signals=[Signal.SELL, Signal.HOLD])

    print("\n=== Test 3: Sideways/flat -> ekspektasi HOLD (ADX rendah) ===")
    run_test("flat", make_flat_candles(), expected_signals=[Signal.HOLD])

    print("\n=== Test 4: Data candle terlalu sedikit -> harus HOLD, tidak error ===")
    run_test("insufficient_data", make_trending_candles(n=10), expected_signals=[Signal.HOLD])

    print("\n=== Test 5 (rolling): Uptrend, require_trend_alignment=True -> SELL harus 0 ===")
    run_rolling_test(
        "uptrend, aligned",
        make_trending_candles(n=150),
        forbidden_signal=Signal.SELL,
        strategy=TrendReversalStrategy(require_trend_alignment=True),
    )

    print("\n=== Test 6 (rolling): Uptrend, require_trend_alignment=False -> SELL BOLEH muncul (reversal murni) ===")
    run_rolling_test(
        "uptrend, unaligned",
        make_trending_candles(n=150),
        forbidden_signal=None,
        strategy=TrendReversalStrategy(require_trend_alignment=False),
    )

    print("\n=== Test 7 (rolling): Downtrend, require_trend_alignment=True -> BUY harus 0 ===")
    run_rolling_test(
        "downtrend, aligned",
        make_downtrending_candles(n=150),
        forbidden_signal=Signal.BUY,
        strategy=TrendReversalStrategy(require_trend_alignment=True),
    )

    print("\nSemua test selesai tanpa exception -> logika dasar aman dari crash.")
