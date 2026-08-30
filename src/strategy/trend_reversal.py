"""
Strategi trend-reversal untuk perps kripto Hyperliquid (Fase 1: rule-based).

Lapisan sinyal (Node 1 & 2: follow trend + reversal) menghasilkan kandidat
entry; lapisan filter ML (Node 3, meta-labeling) menyaring kandidat
berdasarkan probabilitas menang -- direncanakan diintegrasikan lewat field
`confidence` + `min_confidence` di risk manager.

Penyesuaian untuk perps kripto:
- ADX_STRENGTH dinaikkan dari 15 -> 22 (15 terlalu longgar, banyak loloskan
  kondisi choppy sebagai "trending")
- SL/TP berbasis ATR dalam satuan harga aset langsung (bukan "poin" fixed),
  supaya adaptif terhadap perubahan volatilitas kripto
- TP pakai ATR-based RR ratio, bukan target level bulanan -- target lebar
  membuat posisi nyangkut lama dan tergerus funding rate di perps
- Martingale averaging TIDAK dipakai (berbahaya di perps: funding rate
  saat averaging + risiko liquidation)
"""

import pandas as pd
import ta

from src.strategy.base import Strategy, MarketSnapshot, SignalResult, Signal


class TrendReversalStrategy(Strategy):
    def __init__(
        self,
        ema_period: int = 50,
        adx_period: int = 14,
        adx_strength: float = 22.0,   # baseline 15 terlalu longgar di kripto; uji 15 vs 22 di param sweep
        adx_use_di: bool = False,
        rsi_period: int = 14,
        rsi_overbought: float = 65.0,
        rsi_oversold: float = 35.0,
        atr_period: int = 14,
        use_pin_bar: bool = True,
        use_engulfing: bool = True,
        mode: str = "both",  # "follow_only" | "reversal_only" | "both"
        require_trend_alignment: bool = True,
        # True  = reversal BUY hanya jika close > EMA, reversal SELL hanya jika close < EMA
        #         ("buy the dip" / "sell the rally" -- searah trend besar)
        # False = reversal murni berdasar RSI ekstrem + pola candle, tanpa peduli
        #         arah trend besar (reversal murni)
    ):
        self.ema_period = ema_period
        self.adx_period = adx_period
        self.adx_strength = adx_strength
        self.adx_use_di = adx_use_di
        self.rsi_period = rsi_period
        self.rsi_overbought = rsi_overbought
        self.rsi_oversold = rsi_oversold
        self.atr_period = atr_period
        self.use_pin_bar = use_pin_bar
        self.use_engulfing = use_engulfing
        self.mode = mode
        self.require_trend_alignment = require_trend_alignment

    def required_bars(self) -> int:
        # harus sinkron dengan cek di generate_signal (min_bars di bawah)
        min_bars = max(self.ema_period, self.adx_period * 2, self.rsi_period) + 2
        return min_bars

    def _to_df(self, candles: list) -> pd.DataFrame:
        df = pd.DataFrame(candles)
        for col in ["o", "h", "l", "c", "v"]:
            df[col] = df[col].astype(float)
        return df

    def _compute_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        df["ema"] = ta.trend.ema_indicator(df["c"], window=self.ema_period)

        adx_ind = ta.trend.ADXIndicator(df["h"], df["l"], df["c"], window=self.adx_period)
        df["adx"] = adx_ind.adx()
        df["plus_di"] = adx_ind.adx_pos()
        df["minus_di"] = adx_ind.adx_neg()

        df["rsi"] = ta.momentum.RSIIndicator(df["c"], window=self.rsi_period).rsi()
        df["atr"] = ta.volatility.AverageTrueRange(
            df["h"], df["l"], df["c"], window=self.atr_period
        ).average_true_range()
        return df

    @staticmethod
    def _is_pin_bar_bullish(row) -> bool:
        body = abs(row["c"] - row["o"])
        rng = row["h"] - row["l"]
        if rng == 0:
            return False
        upper_shadow = row["h"] - max(row["o"], row["c"])
        lower_shadow = min(row["o"], row["c"]) - row["l"]
        return lower_shadow >= 2.0 * body and upper_shadow <= body

    @staticmethod
    def _is_pin_bar_bearish(row) -> bool:
        body = abs(row["c"] - row["o"])
        rng = row["h"] - row["l"]
        if rng == 0:
            return False
        upper_shadow = row["h"] - max(row["o"], row["c"])
        lower_shadow = min(row["o"], row["c"]) - row["l"]
        return upper_shadow >= 2.0 * body and lower_shadow <= body

    @staticmethod
    def _is_engulfing_bullish(prev_row, cur_row) -> bool:
        prev_bearish = prev_row["c"] < prev_row["o"]
        cur_bullish = cur_row["c"] > cur_row["o"]
        engulfs = cur_row["c"] > prev_row["o"] and cur_row["o"] < prev_row["c"]
        return prev_bearish and cur_bullish and engulfs

    @staticmethod
    def _is_engulfing_bearish(prev_row, cur_row) -> bool:
        prev_bullish = prev_row["c"] > prev_row["o"]
        cur_bearish = cur_row["c"] < cur_row["o"]
        engulfs = cur_row["c"] < prev_row["o"] and cur_row["o"] > prev_row["c"]
        return prev_bullish and cur_bearish and engulfs

    def generate_signal(self, snapshot: MarketSnapshot) -> SignalResult:
        min_bars = max(self.ema_period, self.adx_period * 2, self.rsi_period) + 2
        if len(snapshot.candles) < min_bars:
            return SignalResult(Signal.HOLD, 0.0, f"data belum cukup (butuh >= {min_bars} candle)")

        df = self._to_df(snapshot.candles)
        df = self._compute_indicators(df)

        # bar -1 = candle terakhir yang sudah close (asumsi snapshot tidak
        # menyertakan candle yang masih berjalan; kalau menyertakan, ganti ke -2)
        last = df.iloc[-1]
        prev = df.iloc[-2]

        return self._decide_from_rows(last, prev)

    def _decide_from_rows(self, last, prev) -> SignalResult:
        """Logika keputusan murni dari dua baris indikator yang SUDAH dihitung
        (last = bar dievaluasi, prev = bar sebelumnya). Dipisah dari
        generate_signal() supaya bisa dipakai ulang oleh param_sweep.py tanpa
        recompute indikator per kombinasi (satu-satunya sumber kebenaran logic,
        tidak ada duplikasi -- param_sweep manggil method ini langsung)."""
        if pd.isna(last["ema"]) or pd.isna(last["adx"]) or pd.isna(last["rsi"]) or pd.isna(last["atr"]):
            return SignalResult(Signal.HOLD, 0.0, "indikator belum warm-up penuh")

        follow_buy = follow_sell = reversal_buy = reversal_sell = False
        reason = ""

        # --- NODE 1: FOLLOW TREND ---
        if self.mode in ("follow_only", "both"):
            adx_buy_dir = (last["plus_di"] > last["minus_di"]) if self.adx_use_di else True
            adx_sell_dir = (last["minus_di"] > last["plus_di"]) if self.adx_use_di else True

            if last["c"] > last["ema"] and last["adx"] >= self.adx_strength \
                    and adx_buy_dir and last["rsi"] < self.rsi_overbought:
                follow_buy = True
                reason = f"FOLLOW BUY: close>EMA, ADX={last['adx']:.1f}, RSI={last['rsi']:.1f}"

            if last["c"] < last["ema"] and last["adx"] >= self.adx_strength \
                    and adx_sell_dir and last["rsi"] > self.rsi_oversold:
                follow_sell = True
                reason = f"FOLLOW SELL: close<EMA, ADX={last['adx']:.1f}, RSI={last['rsi']:.1f}"

        # --- NODE 2: REVERSAL ---
        if self.mode in ("reversal_only", "both"):
            trend_is_up = last["c"] > last["ema"]
            trend_is_down = last["c"] < last["ema"]

            if last["rsi"] <= self.rsi_oversold:
                bullish_pattern = (
                    (self.use_pin_bar and self._is_pin_bar_bullish(last))
                    or (self.use_engulfing and self._is_engulfing_bullish(prev, last))
                )
                # kalau require_trend_alignment aktif, reversal BUY cuma boleh
                # searah trend besar (buy the dip); kalau tidak, reversal murni
                # boleh melawan trend (reversal murni)
                trend_ok = trend_is_up if self.require_trend_alignment else True
                if bullish_pattern and trend_ok:
                    reversal_buy = True
                    align_note = "searah EMA" if self.require_trend_alignment else "tanpa filter trend"
                    reason = f"REVERSAL BUY: RSI oversold ({last['rsi']:.1f}) + pola bullish ({align_note})"

            if last["rsi"] >= self.rsi_overbought:
                bearish_pattern = (
                    (self.use_pin_bar and self._is_pin_bar_bearish(last))
                    or (self.use_engulfing and self._is_engulfing_bearish(prev, last))
                )
                trend_ok = trend_is_down if self.require_trend_alignment else True
                if bearish_pattern and trend_ok:
                    reversal_sell = True
                    align_note = "searah EMA" if self.require_trend_alignment else "tanpa filter trend"
                    reason = f"REVERSAL SELL: RSI overbought ({last['rsi']:.1f}) + pola bearish ({align_note})"

        do_buy = follow_buy or reversal_buy
        do_sell = follow_sell or reversal_sell

        if not do_buy and not do_sell:
            return SignalResult(Signal.HOLD, 0.0, "tidak ada kondisi entry terpenuhi")

        # confidence sederhana: follow-trend (ADX kuat) dianggap lebih yakin
        # daripada reversal (melawan arah utama)
        confidence = 0.65 if (follow_buy or follow_sell) else 0.5

        if do_buy:
            return SignalResult(Signal.BUY, confidence, reason)
        return SignalResult(Signal.SELL, confidence, reason)
    