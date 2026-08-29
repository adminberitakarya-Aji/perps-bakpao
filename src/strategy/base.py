from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum


class Signal(Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


@dataclass
class MarketSnapshot:
    symbol: str
    mid_price: float
    candles: list  # list of [t, o, h, l, c, v]


@dataclass
class SignalResult:
    signal: Signal
    confidence: float  # 0.0 - 1.0
    reason: str


class Strategy(ABC):
    """Semua strategi wajib implement ini. Lihat contoh di sma_crossover.py."""

    @abstractmethod
    def generate_signal(self, snapshot: MarketSnapshot) -> SignalResult:
        ...

    def required_bars(self) -> int:
        """Jumlah bar closed minimum yang strategi butuh agar indikator warm-up.

        Dipakai jalur LIVE supaya fetch_snapshot mengambil data secukupnya
        (bug lama: default 50 bar padahal strategi butuh 52 -> selalu HOLD).
        Default konservatif; strategi dengan indikator berat wajib override.
        """
        return 60
