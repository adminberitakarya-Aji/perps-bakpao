"""
Filter sinyal ML untuk live engine & backtest (Fase 2).

Memuat model ONNX produksi (models/btc_ml_rf_1h.onnx) + metadata
(.meta.json), menghitung 16 fitur dari window candle PERSIS seperti
src/ml/export_dataset.py (satu sumber kebenaran dengan training), lalu
memberi probabilitas p(win). Sinyal dieksekusi hanya jika p1 >= threshold
(produksi: 0.60).

Prinsip FAIL-CLOSED: kalau ada error apapun saat inferensi, sinyal DITOLAK
(edge strategi datang dari filter, bukan dari strategi mentah -- tanpa
filter, ekspektansi net-nya negatif).
"""

import json
import os

import numpy as np
import onnxruntime as ort
import pandas as pd

from src.strategy.base import Signal
from src.utils.logger import get_logger

log = get_logger("ml_inference")

DEFAULT_MODEL_PATH = os.path.join("models", "btc_ml_rf_1h.onnx")


class MLSignalFilter:
    def __init__(self, model_path: str = DEFAULT_MODEL_PATH,
                 threshold: float | None = None):
        meta_path = model_path.replace(".onnx", ".meta.json")
        if not os.path.exists(model_path) or not os.path.exists(meta_path):
            raise FileNotFoundError(
                f"Model/metadata tidak ditemukan: {model_path}, {meta_path}")
        with open(meta_path) as f:
            self.meta = json.load(f)
        self.feature_order: list = self.meta["feature_order"]
        self.threshold = threshold if threshold is not None \
            else float(self.meta["decision_rule"]["threshold"])
        self.input_name = self.meta["input"]["name"]
        self.session = ort.InferenceSession(
            model_path, providers=["CPUExecutionProvider"])
        log.info("ML filter siap: %s | %d fitur | threshold %.2f",
                 model_path, len(self.feature_order), self.threshold)

    # ------------------------------------------------------------------
    # Feature engineering -- rumus WAJIB identik export_dataset.py
    # ------------------------------------------------------------------
    def compute_features(self, candles: list, signal: Signal,
                         strategy, funding_rate: float | None = None) -> dict | None:
        """Fitur dari bar sinyal (bar terakhir yang CLOSED di `candles`).

        `strategy` harus punya _to_df() + _compute_indicators() yang
        menghasilkan kolom ema/adx/plus_di/minus_di/rsi/atr (duck-typing,
        sama dengan pattern _get_last_atr di engine).
        """
        if not candles:
            return None
        if len(candles) < 520:
            log.warning("window candle %d < 520 -- fitur regime butuh >= 500 bar "
                        "history (fail-closed)", len(candles))
            return None
        df = strategy._to_df(candles)
        df = strategy._compute_indicators(df)
        # fitur volume: rasio volume vs SMA20/SMA100 (seperti exporter)
        df["vol_sma20"] = df["v"].rolling(20).mean()
        df["vol_sma100"] = df["v"].rolling(100).mean()

        i = len(candles) - 1  # bar sinyal = bar terakhir
        last = df.iloc[i]
        candle = candles[i]
        o, h = float(candle["o"]), float(candle["h"])
        l, c = float(candle["l"]), float(candle["c"])
        v = float(candle["v"])
        atr = float(last["atr"])
        if not atr == atr or atr <= 0 or c <= 0:
            return None

        dt = pd.Timestamp(int(candle["t"]), unit="ms", tz="UTC")
        v20, v100 = float(df["vol_sma20"].iloc[i]), float(df["vol_sma100"].iloc[i])
        feats = {
            "dist_to_ema_atr": (c - float(last["ema"])) / atr,
            "adx_main": float(last["adx"]),
            "adx_pdi": float(last["plus_di"]),
            "adx_mdi": float(last["minus_di"]),
            "adx_di_diff": float(last["plus_di"]) - float(last["minus_di"]),
            "rsi": float(last["rsi"]),
            "atr_normalized": atr / c * 1000.0,
            "body_atr": abs(c - o) / atr,
            "upper_shadow_atr": (h - max(o, c)) / atr,
            "lower_shadow_atr": (min(o, c) - l) / atr,
            "hour": dt.hour,
            "day_of_week": dt.dayofweek,  # 0=Senin (konvensi pandas)
            "signal_type": 1 if signal == Signal.BUY else 2,
            "vol_ratio_20": v / v20 if v20 > 0 else 1.0,
            "vol_ratio_100": v / v100 if v100 > 0 else 1.0,
            "funding_rate": float(funding_rate) if funding_rate is not None else 0.0,
        }
        # fitur regime (iterasi-2) -- rumus identik exporter
        rv = float(df["c"].pct_change().rolling(50).std().iloc[i])
        dhi = float(((df["h"].rolling(500).max() - df["c"]) / df["atr"]).iloc[i])
        dlo = float(((df["c"] - df["l"].rolling(500).min()) / df["atr"]).iloc[i])
        if rv != rv or dhi != dhi or dlo != dlo:
            log.warning("fitur regime NaN (history kurang?) -> fail-closed")
            return None
        feats["rv_50"] = rv
        feats["dist_hi_500_atr"] = dhi
        feats["dist_lo_500_atr"] = dlo
        return feats

    def _to_vector(self, feats: dict) -> np.ndarray:
        missing = [f for f in self.feature_order if f not in feats]
        if missing:
            raise KeyError(f"fitur hilang: {missing}")
        return np.array([[feats[f] for f in self.feature_order]], dtype=np.float32)

    def predict_proba(self, candles: list, signal: Signal, strategy,
                      funding_rate: float | None = None) -> float | None:
        """p(win) dari bar sinyal; None = inferensi gagal (fail-closed)."""
        try:
            feats = self.compute_features(candles, signal, strategy, funding_rate)
            if feats is None:
                log.warning("fitur tidak valid (ATR NaN / harga <= 0)")
                return None
            out = self.session.run(None, {self.input_name: self._to_vector(feats)})
            return float(out[1][0, 1])  # class_order [0,1] -> kolom 1 = p(win)
        except Exception as e:
            log.error("inferensi ML gagal (fail-closed): %s", e)
            return None

    def allow(self, candles: list, signal: Signal, strategy,
              funding_rate: float | None = None) -> bool:
        """True jika sinyal boleh dieksekusi (p1 >= threshold). Fail-closed."""
        if signal not in (Signal.BUY, Signal.SELL):
            return False
        p1 = self.predict_proba(candles, signal, strategy, funding_rate)
        if p1 is None:
            return False
        log.info("ML p(win)=%.3f threshold=%.2f -> %s",
                 p1, self.threshold, "PASS" if p1 >= self.threshold else "SKIP")
        return p1 >= self.threshold
