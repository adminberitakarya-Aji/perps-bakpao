from src.strategy.base import Strategy, MarketSnapshot, SignalResult, Signal


class SMACrossoverStrategy(Strategy):
    """Strategi contoh sederhana. Ganti/tambah strategi lain di folder ini
    tanpa menyentuh engine, risk manager, atau executor."""

    def __init__(self, fast_period: int = 9, slow_period: int = 21):
        self.fast_period = fast_period
        self.slow_period = slow_period

    def generate_signal(self, snapshot: MarketSnapshot) -> SignalResult:
        closes = [float(c["c"]) for c in snapshot.candles]
        if len(closes) < self.slow_period:
            return SignalResult(Signal.HOLD, 0.0, "data candle belum cukup")

        fast_ma = sum(closes[-self.fast_period:]) / self.fast_period
        slow_ma = sum(closes[-self.slow_period:]) / self.slow_period

        if fast_ma > slow_ma:
            return SignalResult(Signal.BUY, 0.6, f"fast MA({fast_ma:.2f}) > slow MA({slow_ma:.2f})")
        if fast_ma < slow_ma:
            return SignalResult(Signal.SELL, 0.6, f"fast MA({fast_ma:.2f}) < slow MA({slow_ma:.2f})")
        return SignalResult(Signal.HOLD, 0.0, "MA berdekatan")
