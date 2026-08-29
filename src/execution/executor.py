from src.client import HyperliquidClient
from src.strategy.base import Signal
from src.utils.logger import get_logger

log = get_logger("exec")


class OrderExecutor:
    """Kirim order final ke Hyperliquid dari jalur live."""

    # Hyperliquid menolak order dengan notional < $10 per asset.
    MIN_NOTIONAL_USD = 10.0

    def __init__(self, client: HyperliquidClient):
        self.client = client

    def execute(
        self,
        symbol: str,
        signal: Signal,
        size_usd: float,
        price: float,
        sl: float | None = None,
        tp: float | None = None,
    ):
        if size_usd < self.MIN_NOTIONAL_USD:
            log.warning("[%s] skip: notional $%.2f < minimum $%.0f", symbol, size_usd, self.MIN_NOTIONAL_USD)
            return None

        size_in_asset = round(size_usd / price, 5)
        if size_in_asset <= 0:
            log.warning("[%s] skip: size %s (asset) terlalu kecil", symbol, size_in_asset)
            return None

        is_buy = signal == Signal.BUY
        proteksi = f" SL={sl} TP={tp}" if (sl is not None or tp is not None) else ""
        log.info("%s %s size=%s (~$%.2f)%s", signal.value, symbol, size_in_asset, size_usd, proteksi)

        try:
            result = self.client.place_market_order(symbol, is_buy, size_in_asset, sl=sl, tp=tp)
            log.info("[%s] Order result: %s", symbol, result)
            return result
        except Exception as e:
            log.error("[%s] Gagal eksekusi order: %s", symbol, e)
            return None
