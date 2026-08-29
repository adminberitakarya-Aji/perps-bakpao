import time

from src.config import Config
from src.client import HyperliquidClient
from src.strategy.trend_reversal import TrendReversalStrategy
from src.risk.manager import RiskManager, RiskLimits
from src.execution.executor import OrderExecutor
from src.engine import TradingEngine
from src.utils.logger import get_logger

POLL_INTERVAL_SECONDS = 60 * 15  # 15 menit, sesuaikan dengan timeframe strategi

log = get_logger("main")


def main():
    config = Config.from_env()
    client = HyperliquidClient(config)

    strategy = TrendReversalStrategy(
        require_trend_alignment=True,  # ganti ke False untuk uji perilaku EA asli (reversal murni)
    )
    risk_manager = RiskManager(RiskLimits())
    executor = OrderExecutor(client)

    engine = TradingEngine(
        client=client,
        strategy=strategy,
        risk_manager=risk_manager,
        executor=executor,
        symbols=["BTC", "ETH"],
    )

    log.info("Agent mulai jalan (%s)", "TESTNET" if config.use_testnet else "MAINNET")

    while True:
        try:
            engine.run_once()
        except Exception as e:
            log.error("Error saat run_once: %s", e)

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
