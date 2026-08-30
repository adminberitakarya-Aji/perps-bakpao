"""
Risk manager punya kata akhir atas setiap sinyal, bukan strategi.
Ini skeleton minimal — kembangkan aturan sesuai toleransi risiko kamu
sebelum dipakai dengan dana asli.
"""

from dataclasses import dataclass

from src.strategy.base import Signal
from src.utils.logger import get_logger

log = get_logger("risk")


@dataclass
class RiskLimits:
    max_position_pct: float = 0.1      # maks 10% equity per posisi
    max_leverage: float = 3.0
    max_daily_loss_pct: float = 0.05   # kill switch otomatis di -5% harian
    min_confidence: float = 0.5        # abaikan sinyal di bawah ini
    atr_sl_mult: float = 2.0           # SL = ATR * mult (adaptif volatilitas, bukan jarak fixed)
    tp_rr_ratio: float = 1.5           # TP = jarak SL * rasio ini (RR fixed, konsisten dgn format label ML)

    # --- Trailing stop ---
    # Semua dalam kelipatan ATR saat entry (bukan jarak fixed),
    # supaya proporsional terhadap volatilitas tiap posisi.
    use_trailing: bool = True
    trailing_start_atr_mult: float = 1.5   # trailing aktif setelah profit >= ATR * mult ini
    trailing_distance_atr_mult: float = 1.2  # jarak SL baru dari harga saat ini
    trailing_step_atr_mult: float = 0.3    # SL cuma digeser kalau pergerakan >= step ini (hindari over-churn)

    # --- Position sizing berbasis risiko (risk percent per trade) ---
    # Notional = (equity * risk_per_trade_pct) / jarak SL (dalam % harga).
    # Sizing lama (max_position_pct * confidence) mengabaikan jarak SL,
    # akibatnya risiko per trade bervariasi liar mengikuti ATR.
    risk_per_trade_pct: float = 0.01      # 1% equity risiko per trade (InpRiskPercent=1.0)
    # cap notional = equity * max_leverage (di bawah). Di perps, risiko 1%
    # dengan SL 1.2% butuh notional ~83% equity -- cap 10% equity akan
    # mematikan sizing risk-based (risiko aktual jadi ~0.1%).


class RiskManager:
    def __init__(self, limits: RiskLimits):
        self.limits = limits
        self.daily_pnl_pct = 0.0  # di-update TradingEngine._update_daily_pnl() tiap run_once (basis hari UTC)

    def check_and_size(
        self,
        equity_usd: float,
        signal: Signal,
        confidence: float,
        sl_distance_pct: float | None = None,
    ) -> float:
        """Return ukuran posisi (notional USD). 0 berarti sinyal ditolak.

        sl_distance_pct = jarak stop loss terhadap entry, dalam pecahan
        harga (mis. 0.012 = 1.2%). Kalau diberikan, sizing berbasis risiko:
        notional = (equity * risk%) / sl_distance_pct,
        di-cap risk_cap_pct * equity. Kalau None, fallback ke sizing lama
        (max_position_pct * confidence) supaya pemanggil lama tidak pecah.
        """
        if self.daily_pnl_pct <= -self.limits.max_daily_loss_pct:
            log.warning("Kill switch aktif: batas rugi harian tercapai")
            return 0.0

        if confidence < self.limits.min_confidence:
            log.info("Sinyal ditolak: confidence %s < %s", confidence, self.limits.min_confidence)
            return 0.0

        if signal == Signal.HOLD:
            return 0.0

        if sl_distance_pct is not None and sl_distance_pct > 0:
            risk_money = equity_usd * self.limits.risk_per_trade_pct
            position_size = risk_money / sl_distance_pct
            max_size = equity_usd * self.limits.max_leverage
            if position_size > max_size:
                log.info("Notional %.2f di-cap ke %.2f (maks %.0fx equity)", position_size, max_size, self.limits.max_leverage)
                position_size = max_size
            return position_size

        # fallback sizing lama (tanpa info jarak SL)
        position_size = equity_usd * self.limits.max_position_pct * confidence
        return position_size

    def compute_sl_tp(self, signal: Signal, entry_price: float, atr: float) -> tuple[float, float]:
        """SL/TP berbasis ATR & harga langsung.
        Return (stop_loss_price, take_profit_price)."""
        sl_distance = atr * self.limits.atr_sl_mult
        tp_distance = sl_distance * self.limits.tp_rr_ratio

        if signal == Signal.BUY:
            sl = entry_price - sl_distance
            tp = entry_price + tp_distance
        else:  # SELL
            sl = entry_price + sl_distance
            tp = entry_price - tp_distance

        return round(sl, 2), round(tp, 2)

    def compute_trailing_sl(
        self,
        signal: Signal,
        entry_price: float,
        current_price: float,
        current_sl: float,
        entry_atr: float,
    ):
        """Trailing stop untuk posisi tunggal.
        Return SL baru (float) kalau perlu digeser, atau None kalau belum
        waktunya / belum cukup pergerakan.

        entry_atr = ATR pada saat posisi dibuka (disimpan di posisi), supaya
        jarak trailing proporsional terhadap volatilitas saat entry, bukan
        volatilitas saat ini yang bisa berubah drastis di tengah posisi."""
        if not self.limits.use_trailing:
            return None

        trail_start_dist = entry_atr * self.limits.trailing_start_atr_mult
        trail_dist = entry_atr * self.limits.trailing_distance_atr_mult
        trail_step = entry_atr * self.limits.trailing_step_atr_mult

        if signal == Signal.BUY:
            profit_dist = current_price - entry_price
            if profit_dist < trail_start_dist:
                return None
            new_sl = current_price - trail_dist
            if new_sl > current_sl + trail_step:
                return round(new_sl, 2)
            return None
        else:  # SELL
            profit_dist = entry_price - current_price
            if profit_dist < trail_start_dist:
                return None
            new_sl = current_price + trail_dist
            if current_sl == 0 or new_sl < current_sl - trail_step:
                return round(new_sl, 2)
            return None
        