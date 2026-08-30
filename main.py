import time

from src.config import Config
from src.client import HyperliquidClient
from src.strategy.trend_reversal import TrendReversalStrategy
from src.risk.manager import RiskManager, RiskLimits
from src.execution.executor import OrderExecutor
from src.engine import TradingEngine
# CATATAN: import MLSignalFilter sengaja LAZY (di dalam main(), hanya saat
# ml_filter_enabled=true). Bot tanpa filter ML tidak butuh onnxruntime/numpy.
from src.utils.logger import get_logger
from src.utils.notifier import TelegramNotifier

POLL_INTERVAL_SECONDS = 60 * 60   # timeframe strategi produksi (BTC 1H)
CANDLE_CLOSE_BUFFER_SECONDS = 300  # jeda setelah close candle (data siap di API)

KILL_SWITCH_CHECK_SECONDS = 60  # monitoring kill switch antar-poll (fix P2-10)

log = get_logger("main")


def seconds_until_next_poll(
    interval_seconds: int, buffer_s: int = CANDLE_CLOSE_BUFFER_SECONDS, now: float | None = None
) -> float:
    """Detik sampai poll berikutnya yang SELARAS boundary candle.

    Sinyal berasal dari candle yang close di boundary (mis. 01:00:00 untuk
    1H). Entry harus terjadi sesegera mungkin setelah close + buffer kecil,
    BUKAN interval tetap dari waktu start proses (bug lama: poll bisa jatuh
    di tengah candle -> entry telat sampai ~1 jam, mid price sudah beda dari
    asumsi backtest). Parameter `now` untuk testability.
    """
    if now is None:
        now = time.time()
    # target terdekat: boundary candle TERAKHIR + buffer kalau masih di depan,
    # kalau tidak boundary BERIKUTNYA + buffer (jangan sampai melompati siklus)
    cur_target = int(now // interval_seconds) * interval_seconds + buffer_s
    target = cur_target if now < cur_target else cur_target + interval_seconds
    return max(target - now, 1.0)


# Status validasi venue (docs/go_live_validation.md): BELUM lolos go-live.
# Integrasi ini sudah lengkap & teruji, tapi jangan aktifkan ml_filter
# (ML_FILTER_ENABLED=true) sebelum model tervalidasi ulang di data HL.

def main():
    config = Config.from_env()
    client = HyperliquidClient(config)
    notifier = TelegramNotifier(config.telegram_bot_token, config.telegram_chat_id)

    # KONFIGURASI PRODUKSI (hasil walk-forward Fase 2): kandidat tanpa filter
    # trend (require_trend_alignment=False) -- WAJIB sama dengan exporter
    # dataset ML; kalau beda, distribusi fitur training tidak berlaku.
    strategy = TrendReversalStrategy(
        require_trend_alignment=False,
    )
    risk_manager = RiskManager(RiskLimits())
    executor = OrderExecutor(client, notifier)

    # Filter ML produksi: RF ONNX BTC 1H, threshold 0.60 (fail-closed).
    ml_filter = None
    if config.ml_filter_enabled:
        try:
            # Lazy import: butuh onnxruntime + numpy (ada di requirements.txt).
            # ImportError pun tertangkap di bawah -> fail-closed (bot tidak jalan
            # dengan strategi mentah yang E[r_net]-nya negatif).
            from src.ml.inference import MLSignalFilter

            model_path = config.ml_model_path or None
            if model_path:
                ml_filter = MLSignalFilter(model_path, threshold=config.ml_threshold)
            else:
                ml_filter = MLSignalFilter(threshold=config.ml_threshold)
            log.info("Filter ML aktif (threshold %.2f)", config.ml_threshold)
        except Exception as e:
            # fail-closed juga di sini: tanpa model, strategi mentah rugi
            # (E[r_net] negatif), jadi JANGAN jalan tanpa filter.
            log.error("Gagal muat filter ML: %s -- bot TIDAK dijalankan", e)
            raise SystemExit(1)

    engine = TradingEngine(
        client=client,
        strategy=strategy,
        risk_manager=risk_manager,
        executor=executor,
        symbols=["BTC"],  # produksi: BTC 1H (model dilatih khusus BTC)
        notifier=notifier,
        ml_filter=ml_filter,
    )

    log.info("Agent mulai jalan (%s)", "TESTNET" if config.use_testnet else "MAINNET")

    while True:
        try:
            engine.run_once()
        except Exception as e:
            log.error("Error saat run_once: %s", e)
            notifier.notify_error("di run_once", e)

        # Sleep SELARAS boundary candle (+buffer), bukan interval tetap dari
        # waktu start proses -> eksekusi live konsisten dengan asumsi backtest
        # (entry di dekat close candle sinyal). Selama menunggu, monitor kill
        # switch tiap menit supaya alert & blokir entry tidak telat 1 jam
        # (fix P2-10).
        next_poll = time.time() + seconds_until_next_poll(POLL_INTERVAL_SECONDS)
        while True:
            remaining = next_poll - time.time()
            if remaining <= 0:
                break
            time.sleep(min(KILL_SWITCH_CHECK_SECONDS, remaining))
            try:
                engine.monitor_kill_switch()
            except Exception as e:
                log.error("Error saat monitor kill switch: %s", e)


if __name__ == "__main__":
    main()
