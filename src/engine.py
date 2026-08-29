import json
import os
from datetime import datetime, timezone

from src.client import HyperliquidClient
from src.data.market_data import fetch_snapshot
from src.strategy.base import MarketSnapshot, Signal, Strategy
from src.risk.manager import RiskManager
from src.execution.executor import OrderExecutor
from src.utils.logger import get_logger
from src.utils.notifier import TelegramNotifier

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
        notifier: TelegramNotifier | None = None,
    ):
        self.client = client
        self.strategy = strategy
        self.risk_manager = risk_manager
        self.executor = executor
        self.symbols = symbols
        self.interval = interval
        # notifier default = no-op (mode silent) supaya test/wiring lama tidak pecah
        self.notifier = notifier or TelegramNotifier()
        self.state_path = os.path.join("data", "live_positions.json")
        self.live_positions: dict = {}  # symbol -> {"side", "entry_price", "entry_atr", "sl", "tp"}
        self.daily_state_path = os.path.join("data", "daily_state.json")
        self.daily_state: dict = {}  # {"date_utc", "day_start_equity", "kill_triggered"}
        self._load_state()
        self._load_daily_state()

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

    # ------------------------------------------------------------------
    # Tracker PnL harian (kill switch risk manager) -- persist + reset UTC
    # ------------------------------------------------------------------
    def _load_daily_state(self):
        try:
            if os.path.exists(self.daily_state_path):
                with open(self.daily_state_path) as f:
                    self.daily_state = json.load(f)
        except Exception as e:
            log.error("gagal memuat daily state: %s", e)
            self.daily_state = {}

    def _save_daily_state(self):
        try:
            os.makedirs(os.path.dirname(self.daily_state_path), exist_ok=True)
            with open(self.daily_state_path, "w") as f:
                json.dump(self.daily_state, f, indent=1)
        except Exception as e:
            log.error("gagal menyimpan daily state: %s", e)

    def _get_equity_or_none(self) -> float | None:
        """Equity asli (marginSummary.accountValue); None kalau kosong/gagal.

        Dipakai tracker harian: nilai fallback TIDAK boleh dipakai di sini
        supaya PnL harian tidak pernah dihitung dari angka palsu.
        """
        try:
            state = self.client.get_account_state()
            account_value = float(state.get("marginSummary", {}).get("accountValue", 0) or 0)
            if account_value <= 0:
                return None
            return account_value
        except Exception as e:
            log.warning("gagal ambil account state: %s", e)
            return None

    def _update_daily_pnl(self):
        """Hitung PnL harian (basis hari UTC) -> suntikkan ke risk_manager.

        - Ganti hari UTC -> day_start_equity di-reset (baseline baru hari itu).
        - Equity tidak tersedia (wallet kosong / API gagal) -> tracker TIDAK
          di-update (nilai terakhir dipertahankan, supaya kill switch tidak
          dibuka/ditutup oleh data kosong -> tidak ada PnL palsu).
        - Kill switch memblokir ENTRY baru saja; posisi terbuka tetap
          dikelola penuh (SL/TP/trailing tetap jalan).
        """
        equity = self._get_equity_or_none()
        if equity is None:
            log.info("equity tidak tersedia -> tracker PnL harian tidak di-update")
            return

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        if self.daily_state.get("date_utc") != today:
            # PnL final "kemarin" = equity saat rollover vs baseline kemarin
            yesterday_pnl_pct = None
            old_baseline = float(self.daily_state.get("day_start_equity") or 0)
            if old_baseline > 0:
                yesterday_pnl_pct = (equity - old_baseline) / old_baseline

            self.daily_state = {
                "date_utc": today,
                "day_start_equity": equity,
                "kill_triggered": False,
            }
            self._save_daily_state()
            log.info("tracker harian reset: date=%s baseline_equity=%.2f", today, equity)

            # heartbeat = bukti bot hidup + ringkasan awal hari (kirim SEKALI
            # per hari, terikat deteksi rollover yang persisted)
            try:
                positions = []
                for sym, st in self.live_positions.items():
                    pos = self.client.get_position(sym)
                    size = abs(pos["szi"]) if pos else None
                    positions.append((sym, st.get("side"), size, st.get("entry_price"), st.get("sl"), st.get("tp")))
                self.notifier.notify_heartbeat(
                    today,
                    equity,
                    yesterday_pnl_pct,
                    positions,
                    False,
                    self.client.config.use_testnet,
                )
            except Exception as e:
                log.warning("gagal kirim heartbeat: %s", e)

        day_start = float(self.daily_state.get("day_start_equity") or 0)
        if day_start <= 0:  # state korup -> pulihkan baseline
            self.daily_state["day_start_equity"] = equity
            self._save_daily_state()
            day_start = equity

        daily_pnl_pct = (equity - day_start) / day_start
        self.risk_manager.daily_pnl_pct = daily_pnl_pct
        log.info("equity=%.2f baseline=%.2f daily_pnl=%.2f%%", equity, day_start, daily_pnl_pct * 100)

        if (
            daily_pnl_pct <= -self.risk_manager.limits.max_daily_loss_pct
            and not self.daily_state.get("kill_triggered")
        ):
            self.daily_state["kill_triggered"] = True
            self._save_daily_state()
            log.warning(
                "KILL SWITCH TERPICU: daily_pnl=%.2f%% <= -%.1f%% (entry baru diblokir sampai ganti hari UTC)",
                daily_pnl_pct * 100,
                self.risk_manager.limits.max_daily_loss_pct * 100,
            )
            self.notifier.notify_kill_switch(
                daily_pnl_pct,
                self.risk_manager.limits.max_daily_loss_pct,
                equity,
                day_start,
            )

    def run_once(self):
        # PnL harian (kill switch) di-update SEKALI per run, bukan per symbol
        self._update_daily_pnl()

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
                    size_asset = round(size_usd / snapshot.mid_price, 5)
                    self.notifier.notify_entry(
                        symbol=symbol,
                        signal=result.signal.value,
                        size=size_asset,
                        size_usd=size_usd,
                        price=snapshot.mid_price,
                        sl=sl,
                        tp=tp,
                        confidence=result.confidence,
                        equity=None if equity_usd == 1000.0 and self._get_equity_or_none() is None else equity_usd,
                        reason=result.reason,
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
                self.notifier.notify_closed(
                    symbol,
                    state.get("side", "?"),
                    self._get_equity_or_none(),
                    self.risk_manager.daily_pnl_pct,
                    bool(self.daily_state.get("kill_triggered")),
                )
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
            old_sl = state["sl"]
            try:
                self.client.cancel_all_trigger_orders(symbol)
                close_is_buy = state["side"] == "S"
                self.client.place_tpsl_pair(symbol, close_is_buy, abs(pos["szi"]), new_sl, state.get("tp"))
                log.info("[%s] TRAILING: SL %s -> %s (mid=%s)", symbol, old_sl, new_sl, best_px)
                state["sl"] = new_sl
                self._save_state()
                self.notifier.notify_trailing(symbol, old_sl, new_sl, best_px)
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
                    self.notifier.notify_force_close_trailing(symbol, state.get("sl"), best_px)

    def _get_equity_usd(self) -> float:
        """Equity untuk SIZING; fallback 1000 kalau kosong/gagal.

        Tracker PnL harian memakai _get_equity_or_none() -- jangan pernah
        hitung kill switch dari nilai fallback ini.
        """
        equity = self._get_equity_or_none()
        if equity is None:
            log.warning("equity tidak tersedia -> fallback 1000 untuk sizing")
            return 1000.0
        return equity

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
