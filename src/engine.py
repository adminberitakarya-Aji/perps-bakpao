import json
import os

from src.client import HyperliquidClient
from src.data.market_data import fetch_snapshot
from src.strategy.base import MarketSnapshot, Signal, Strategy
from src.risk.manager import RiskManager
from src.execution.executor import OrderExecutor
from src.utils.logger import get_logger

log = get_logger("engine")


class TradingEngine:
    def __init__(
        self,
        client: HyperliquidClient,
        strategy: Strategy,
        risk_manager: RiskManager,
        executor: OrderExecutor,
        symbols: list,
        interval: str = "1h",
    ):
        self.client = client
        self.strategy = strategy
        self.risk_manager = risk_manager
        self.executor = executor
        self.symbols = symbols
        self.interval = interval
        self.state_path = os.path.join("data", "live_positions.json")
        self.live_positions: dict = {}  # symbol -> {"side", "entry_price", "entry_atr", "sl", "tp"}
        self._load_state()

    # ------------------------------------------------------------------
    # Persistensi state posisi live (trailing tetap benar setelah restart)
    # ------------------------------------------------------------------
    def _load_state(self):
        try:
            if os.path.exists(self.state_path):
                with open(self.state_path) as f:
                    self.live_positions = json.load(f)
                if self.live_positions:
                    log.info("state posisi live dimuat: %s", list(self.live_positions.keys()))
        except Exception as e:
            log.error("gagal memuat state: %s", e)
            self.live_positions = {}

    def _save_state(self):
        try:
            os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
            with open(self.state_path, "w") as f:
                json.dump(self.live_positions, f, indent=1)
        except Exception as e:
            log.error("gagal menyimpan state: %s", e)

    def run_once(self):
        for symbol in self.symbols:
            # --- A. Kelola posisi terbuka dulu (trailing + cleanup) ---
            self._manage_open_positions(symbol)

            # fetch secukupnya sesuai kebutuhan strategi (bug lama: fetch
            # 50 bar padahal strategi butuh 52 -> selalu HOLD di live)
            need = self.strategy.required_bars()
            lookback = max(need + 10, 60)
            snapshot = fetch_snapshot(self.client, symbol, interval=self.interval, lookback_candles=lookback)

            # guard: jangan entry kalau masih ada posisi terbuka di simbol ini
            # (EA asli pakai MaxOpenPositions=1; backtest juga single-position)
            pos = self.client.get_position(symbol)
            if pos is not None:
                log.info("[%s] skip entry: masih ada posisi terbuka (%s, szi=%s)", symbol, pos["side"], pos["szi"])
                continue

            result = self.strategy.generate_signal(snapshot)

            log.info("[%s] sinyal=%s conf=%s alasan=%s", symbol, result.signal.value, result.confidence, result.reason)

            if result.signal == Signal.HOLD:
                continue

            equity_usd = self._get_equity_usd()
            sl_distance_pct = self._get_sl_distance_pct(snapshot)
            size_usd = self.risk_manager.check_and_size(
                equity_usd, result.signal, result.confidence, sl_distance_pct=sl_distance_pct
            )

            if size_usd > 0:
                atr = self._get_last_atr(snapshot)
                if atr is None or atr <= 0:
                    log.warning("[%s] ATR tidak tersedia -> skip entry (butuh SL/TP valid)", symbol)
                    continue
                sl, tp = self.risk_manager.compute_sl_tp(result.signal, snapshot.mid_price, atr)
                exec_result = self.executor.execute(
                    symbol, result.signal, size_usd, snapshot.mid_price, sl=sl, tp=tp
                )
                if exec_result is not None:
                    self.live_positions[symbol] = {
                        "side": "B" if result.signal == Signal.BUY else "S",
                        "entry_price": snapshot.mid_price,
                        "entry_atr": atr,
                        "sl": sl,
                        "tp": tp,
                    }
                    self._save_state()
                    log.info(
                        "posisi %s dicatat: side=%s SL=%s TP=%s",
                        symbol, self.live_positions[symbol]["side"], sl, tp,
                    )

    # ------------------------------------------------------------------
    # Manajemen posisi terbuka: trailing SL + cleanup orphan trigger
    # ------------------------------------------------------------------
    def _manage_open_positions(self, symbol: str):
        state = self.live_positions.get(symbol)
        pos = self.client.get_position(symbol)

        # posisi sudah tertutup (SL/TP terisi atau manual) -> bersihkan
        if pos is None:
            if state is not None:
                log.info("[%s] posisi sudah tertutup -> hapus state & cancel trigger sisa", symbol)
                self.live_positions.pop(symbol, None)
                self._save_state()
            try:
                self.client.cancel_all_trigger_orders(symbol)
            except Exception as e:
                log.warning("[%s] cleanup trigger gagal: %s", symbol, e)
            return

        # posisi ada tapi state hilang (entry manual / crash sebelum save):
        # rekonstruksi state minimal supaya cleanup & guard tetap bekerja.
        # Trailing TIDAK aktif untuk posisi tanpa entry_atr.
        if state is None:
            log.warning("[%s] posisi terbuka tanpa state -> rekonstruksi state minimal (trailing nonaktif untuk posisi ini)", symbol)
            self.live_positions[symbol] = {
                "side": pos["side"],
                "entry_price": pos["entryPx"],
                "entry_atr": None,
                "sl": None,
                "tp": None,
            }
            self._save_state()
            return

        # --- trailing stop (ala risk_manager.compute_trailing_sl) ---
        if self.risk_manager.limits.use_trailing and state.get("entry_atr") and state.get("sl") is not None:
            signal = Signal.BUY if state["side"] == "B" else Signal.SELL
            entry_price = state["entry_price"]
            entry_atr = state["entry_atr"]

            trigger_active = self.client.get_trigger_orders(symbol)
            sl_active = next((o for o in trigger_active if str(o.get("triggerCondition", "")).startswith("sl")), None)
            if sl_active is None:
                log.error("[%s] SL trigger tidak ditemukan di exchange -> PERIKSA MANUAL (posisi mungkin telanjang)", symbol)
                return

            best_px = self.client.get_mid_price(symbol)
            new_sl = self.risk_manager.compute_trailing_sl(
                signal, entry_price, best_px, state["sl"], entry_atr
            )
            if new_sl is None or abs(new_sl - state["sl"]) < 1e-9:
                return  # belum saatnya / pergeseran belum melewati step

            # cancel pair lama -> pasang pair baru (SL digeser, TP dipertahankan)
            try:
                self.client.cancel_all_trigger_orders(symbol)
                close_is_buy = state["side"] == "S"
                self.client.place_tpsl_pair(symbol, close_is_buy, abs(pos["szi"]), new_sl, state.get("tp"))
                log.info("[%s] TRAILING: SL %s -> %s (mid=%s)", symbol, state["sl"], new_sl, best_px)
                state["sl"] = new_sl
                self._save_state()
            except Exception as e:
                log.error("[%s] gagal geser SL: %s -> coba pulihkan pair lama", symbol, e)
                try:
                    close_is_buy = state["side"] == "S"
                    self.client.place_tpsl_pair(symbol, close_is_buy, abs(pos["szi"]), state["sl"], state.get("tp"))
                    log.info("[%s] pair lama dipulihkan (SL=%s)", symbol, state["sl"])
                except Exception as e2:
                    log.error("[%s] pulihkan gagal juga (%s) -> TUTUP PAKSA posisi", symbol, e2)
                    try:
                        self.client.cancel_all_trigger_orders(symbol)
                        self.client.market_close_position(symbol)
                    except Exception as e3:
                        log.critical("[%s] tutup paksa gagal (%s) -- PERIKSA MANUAL!", symbol, e3)

    def _get_equity_usd(self) -> float:
        """Equity (accountValue) dari marginSummary; fallback 1000 kalau kosong/gagal."""
        try:
            state = self.client.get_account_state()
            account_value = float(state.get("marginSummary", {}).get("accountValue", 0) or 0)
            if account_value <= 0:
                log.warning("account state kosong (accountValue=0) -> fallback equity 1000")
                return 1000.0
            return account_value
        except Exception as e:
            log.warning("gagal ambil account state: %s -> fallback equity 1000", e)
            return 1000.0

    def _get_last_atr(self, snapshot: MarketSnapshot) -> float | None:
        """ATR terakhir dari candle closed (untuk SL/TP entry live)."""
        try:
            strategy = self.strategy
            if hasattr(strategy, "_to_df") and hasattr(strategy, "_compute_indicators"):
                df = strategy._to_df(snapshot.candles)
                df = strategy._compute_indicators(df)
                last_atr = df["atr"].iloc[-1]
                if last_atr == last_atr:  # NaN check
                    return float(last_atr)
        except Exception as e:
            log.warning("gagal hitung ATR: %s", e)
        return None

    def _get_sl_distance_pct(self, snapshot: MarketSnapshot) -> float | None:
        """Jarak SL entry sebagai pecahan harga (sl_mult * ATR / price).

        Sama seperti risk_manager.compute_sl_tp: SL = ATR * atr_sl_mult.
        Diambil dari strategi kalau punya indikator ATR, via helper
        _get_last_atr pattern; kalau tidak ada, None -> fallback sizing lama.
        """
        try:
            strategy = self.strategy
            if hasattr(strategy, "_to_df") and hasattr(strategy, "_compute_indicators"):
                df = strategy._to_df(snapshot.candles)
                df = strategy._compute_indicators(df)
                last_atr = df["atr"].iloc[-1]
                if last_atr == last_atr:  # NaN check
                    atr = float(last_atr)
                    if atr > 0:
                        return (atr * self.risk_manager.limits.atr_sl_mult) / snapshot.mid_price
        except Exception as e:
            log.warning("gagal hitung ATR untuk sizing: %s", e)
        return None
