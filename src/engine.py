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
        ml_filter=None,  # MLSignalFilter | None; None = tanpa filter ML
    ):
        self.client = client
        self.strategy = strategy
        self.risk_manager = risk_manager
        self.executor = executor
        self.symbols = symbols
        self.interval = interval
        self.ml_filter = ml_filter
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
                    bool(self.daily_state.get("kill_triggered")),
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
            # fitur ML butuh window panjang: regime 500 bar + vol SMA100 + buffer
            lookback = max(need + 10, 560)
            snapshot = fetch_snapshot(self.client, symbol, interval=self.interval, lookback_candles=lookback)

            # guard: jangan entry kalau masih ada posisi terbuka di simbol ini
            # (satu posisi per simbol; backtest juga single-position)
            pos = self.client.get_position(symbol)
            if pos is not None:
                log.info("[%s] skip entry: masih ada posisi terbuka (%s, szi=%s)", symbol, pos["side"], pos["szi"])
                continue

            result = self.strategy.generate_signal(snapshot)

            log.info("[%s] sinyal=%s conf=%s alasan=%s", symbol, result.signal.value, result.confidence, result.reason)

            if result.signal == Signal.HOLD:
                continue

            # --- Filter ML (fail-closed): p(win) >= threshold, else skip ---
            if self.ml_filter is not None:
                funding_rate = self.client.get_funding_rate(symbol)
                window = snapshot.candles if hasattr(snapshot, "candles") else []
                if not self.ml_filter.allow(window, result.signal,
                                            self.strategy, funding_rate):
                    log.info("[%s] sinyal %s DITOLAK filter ML (fail-closed)",
                             symbol, result.signal.value)
                    continue

            # Indikator dihitung SEKALI per siklus (fix P2-15: sebelumnya
            # _to_df/_compute_indicators dijalankan 3x -- untuk sl_distance,
            # ATR, dan internal strategi).
            atr = self._get_last_atr(snapshot)
            sl_distance_pct = None
            if atr is not None and atr > 0 and snapshot.mid_price > 0:
                sl_distance_pct = (atr * self.risk_manager.limits.atr_sl_mult) / snapshot.mid_price

            # Fail-closed: sizing HANYA dari equity ASLI. Tanpa fallback angka
            # palsu -- equity fiktif (mis. 1000 saat saldo 100) menghasilkan
            # notional oversize -> risiko liquidation.
            equity_usd = self._get_equity_or_none()
            if equity_usd is None:
                log.error("[%s] equity tidak tersedia -> skip entry (fail-closed)", symbol)
                continue
            size_usd = self.risk_manager.check_and_size(
                equity_usd, result.signal, result.confidence, sl_distance_pct=sl_distance_pct
            )

            if size_usd > 0:
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
                        equity=equity_usd,
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
            # alert: trailing mati permanen untuk posisi ini (fix P2-16) --
            # user perlu tahu proteksi live sekarang hanya SL/TP pair di
            # exchange (kalau ada) tanpa penguncian profit otomatis.
            self.notifier.notify_error(
                f"Posisi {symbol} terbuka TANPA state (entry manual/crash?) -> "
                f"state direkonstruksi, trailing NONAKTIF untuk posisi ini "
                f"(side={pos['side']}, szi={pos['szi']})",
                Exception("position state missing"),
            )
            return

        # --- SL guard (self-healing): posisi TIDAK boleh telanjang ---
        # SL trigger wajib ada di exchange untuk state yang punya SL. Kalau
        # hilang (cancel parsial saat trailing, crash di antara cancel-replace,
        # dsb.): pasang ulang pair dari state -> kalau gagal juga, tutup paksa
        # + alert. Jangan pernah cuma log dan membiarkan posisi tanpa SL.
        trigger_active = self.client.get_trigger_orders(symbol)
        sl_active = next((o for o in trigger_active if str(o.get("triggerCondition", "")).startswith("sl")), None)
        if state.get("sl") is not None and sl_active is None:
            log.error("[%s] SL trigger hilang di exchange -> pasang ulang dari state (SL=%s)", symbol, state["sl"])
            try:
                close_is_buy = state["side"] == "S"
                self.client.place_tpsl_pair(symbol, close_is_buy, abs(pos["szi"]), state["sl"], state.get("tp"))
                log.info("[%s] SL dipasang ulang: SL=%s TP=%s", symbol, state["sl"], state.get("tp"))
                self.notifier.notify_error(
                    f"SL {symbol} hilang di exchange & sudah dipasang ulang (SL={state['sl']})",
                    Exception("SL trigger missing"),
                )
                return  # pair baru terpasang; trailing dilanjutkan siklus berikutnya
            except Exception as e:
                log.critical("[%s] gagal pasang ulang SL (%s) -> TUTUP PAKSA posisi", symbol, e)
                try:
                    self.client.cancel_all_trigger_orders(symbol)
                    self.client.market_close_position(symbol)
                except Exception as e2:
                    log.critical("[%s] tutup paksa gagal (%s) -- PERIKSA MANUAL!", symbol, e2)
                self.notifier.notify_force_close(
                    symbol,
                    f"{state.get('side', '?')} {abs(pos['szi'])} {symbol} (SL hilang, re-place gagal)",
                    detail=str(e),
                )
                return

        # --- trailing stop (ala risk_manager.compute_trailing_sl) ---
        if self.risk_manager.limits.use_trailing and state.get("entry_atr") and state.get("sl") is not None:
            signal = Signal.BUY if state["side"] == "B" else Signal.SELL
            entry_price = state["entry_price"]
            entry_atr = state["entry_atr"]

            best_px = self.client.get_mid_price(symbol)
            new_sl = self.risk_manager.compute_trailing_sl(
                signal, entry_price, best_px, state["sl"], entry_atr
            )
            if new_sl is None or abs(new_sl - state["sl"]) < 1e-9:
                return  # belum saatnya / pergeseran belum melewati step

            # modify SL in-place (P2-9): TIDAK ada cancel->replace, jadi tidak
            # ada window tanpa SL. TP tidak berubah -> tidak disentuh.
            old_sl = state["sl"]
            close_is_buy = state["side"] == "S"
            try:
                self.client.modify_sl_trigger(
                    symbol, sl_active["oid"], close_is_buy, abs(pos["szi"]), new_sl
                )
                log.info("[%s] TRAILING: SL %s -> %s (mid=%s, modify)", symbol, old_sl, new_sl, best_px)
                state["sl"] = new_sl
                self._save_state()
                self.notifier.notify_trailing(symbol, old_sl, new_sl, best_px)
            except Exception as e:
                log.error("[%s] gagal modify SL: %s -> fallback cancel+replace pair lama", symbol, e)
                try:
                    self.client.cancel_all_trigger_orders(symbol)
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

    def monitor_kill_switch(self):
        """Cek PnL harian/kill switch DI LUAR siklus poll utama (fix P2-10).

        Dipanggil berkala (tiap ~60 detik) dari loop utama supaya kill switch
        terpicu & alert terkirim segera, bukan menunggu poll 1 jam berikutnya.
        Idempotent: rollover & heartbeat tetap terikat deteksi pergantian hari
        (persisted), jadi tidak ada notifikasi ganda. Kill switch hanya
        MEMBLOKIR entry baru; posisi terbuka tetap dikelola SL/TP/trailing.
        """
        self._update_daily_pnl()

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
        """DEPRECATED: dipertahankan hanya sebagai util; jarak SL entry
        sekarang dihitung SEKALI per siklus di run_once (fix P2-15)."""
        try:
            strategy = self.strategy
            if hasattr(strategy, "_to_df") and hasattr(strategy, "_compute_indicators"):
                df = strategy._to_df(snapshot.candles)
                df = strategy._compute_indicators(df)
                last_atr = df["atr"].iloc[-1]
                if last_atr == last_atr:  # NaN check
                    atr = float(last_atr)
                    if atr > 0 and snapshot.mid_price > 0:
                        return (atr * self.risk_manager.limits.atr_sl_mult) / snapshot.mid_price
        except Exception as e:
            log.warning("gagal hitung ATR untuk sizing: %s", e)
        return None
