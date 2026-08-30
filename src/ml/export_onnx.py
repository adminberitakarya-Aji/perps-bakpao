"""
Ekspor model produksi ke ONNX untuk EA (Fase 2).

Konfigurasi produksi TERKUNCI (hasil walk-forward 3 tahun, lihat
data/btc_1h3y_ml_dataset_wf_results.csv):
  - Aset/TF   : BTC 1H
  - Model     : RandomForest hyperparameter blueprint (docs/train_model.py)
  - Fitur     : 13 fitur MQ5 + vol_ratio_20, vol_ratio_100, funding_rate
  - Keputusan : take-profit probabilitas p1 >= 0.60
  - Eksekusi  : entry taker di close bar sinyal
  - Risiko    : SL 2.0 x ATR, TP 1.5 R (risk:reward), horizon label 30 bar

    python -m src.ml.export_onnx --dataset data/btc_1h3y_ml_dataset.csv

Output:
  models/btc_ml_rf_1h.onnx       -- model (input float [None, NFeat])
  models/btc_ml_rf_1h.meta.json  -- metadata: urutan fitur, threshold,
                                    konfigurasi risiko, statistik WF
Paritas sklearn vs onnxruntime diverifikasi baris-per-baris di sini.
"""

import argparse
import json
import os

import numpy as np
import onnxruntime as ort
import pandas as pd
import skl2onnx
import sklearn
from skl2onnx import convert_sklearn
from skl2onnx.common.data_types import FloatTensorType

from src.ml.train_model import make_model, select_features, load_dataset

THRESHOLD = 0.60
CONFIG = {
    "symbol": "BTC",
    "interval": "1h",
    "entry": "taker",
    "sl_atr_mult": 2.0,
    "tp_rr_ratio": 1.5,
    "label_horizon_bars": 30,
    "adx_strength": 15.0,
    "rsi_oversold": 35.0,
    "rsi_overbought": 65.0,
    "ema_period": 50,
    "indicator_periods": {"adx": 14, "rsi": 14, "atr": 14},
}


def summarize_wf(results_csv: str) -> dict | None:
    """Agregat walk-forward utk model rf @ threshold terkunci (n>=20/fold)."""
    if not os.path.exists(results_csv):
        return None
    r = pd.read_csv(results_csv)
    r = r[(r["model"] == "rf") & (r["threshold"] == THRESHOLD) & (r["n"] >= 20)]
    if r.empty:
        return None
    n = int(r["n"].sum())
    return {
        "n_trades_wf": n,
        "expected_r_net": round(float((r["exp_net"] * r["n"]).sum() / n), 4),
        "folds": int(len(r)),
        "folds_positive_expectancy": int((r["exp_net"] > 0).sum()),
        "note": "anchored-expanding 5 fold, purge 30 bar; E = mean r_net sim "
                "(net-of-cost, sudah termasuk biaya sesungguhnya)",
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="data/btc_1h3y_ml_dataset.csv")
    ap.add_argument("--out", default="models/btc_ml_rf_1h.onnx")
    ap.add_argument("--threshold", type=float, default=THRESHOLD)
    ap.add_argument("--target", choices=["label_win", "label_trail"],
                    default="label_trail",
                    help="label_trail = model utk eksekusi trailing (produksi)")
    args = ap.parse_args()

    df = load_dataset(args.dataset)
    feats = select_features(df)
    X = df[feats].astype(np.float32)
    y = df[args.target].astype(int)
    print(f"Dataset: {args.dataset} | {len(df)} baris | {len(feats)} fitur | target={args.target}")

    # --- Latih model produksi di SELURUH data (evaluasi honest ada di WF) ---
    model = make_model("rf")
    model.fit(X, y)
    proba_sk = model.predict_proba(X)[:, 1]
    print(f"RF terlatih. In-sample AUC: {sklearn.metrics.roc_auc_score(y, proba_sk):.4f}")

    # --- Konversi ONNX (opset 12, zipmap off -> output [prob_class0, prob_class1]) ---
    onnx_model = convert_sklearn(
        model,
        initial_types=[("float_input", FloatTensorType([None, len(feats)]))],
        target_opset=12,
        options={id(model): {"zipmap": False}},
    )
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "wb") as f:
        f.write(onnx_model.SerializeToString())
    print(f"ONNX tersimpan: {args.out} ({os.path.getsize(args.out)} bytes)")

    # --- Verifikasi paritas baris-per-baris ---
    sess = ort.InferenceSession(args.out, providers=["CPUExecutionProvider"])
    ort_out = sess.run(None, {"float_input": X.values})[1]  # [1] = probabilities
    max_diff = float(np.abs(ort_out[:, 1] - proba_sk).max())
    print(f"Paritas predict_proba vs onnxruntime: max|diff| = {max_diff:.2e}")
    if max_diff > 1e-5:
        raise SystemExit("GAGAL: deviasi paritas ONNX > 1e-5 -- jangan deploy!")

    # --- Metadata utk EA ---
    wf_suffix = "_trail" if args.target == "label_trail" else ""
    wf = summarize_wf(args.dataset.replace(".csv", f"_wf{wf_suffix}_results.csv"))
    meta = {
        "model": "RandomForestClassifier",
        "framework": {"sklearn": sklearn.__version__, "skl2onnx": skl2onnx.__version__,
                      "onnxruntime": ort.__version__, "onnx_opset": 12},
        "input": {"name": "float_input", "shape": [None, len(feats)], "dtype": "float32"},
        "output_probabilities_index": 1,
        "class_order": [0, 1],   # kolom prob: [p_loss, p_win]
        "feature_order": feats,  # URUTAN PENTING -- EA harus mengisi persis ini
        "decision_rule": {
            "signal": "proba[:,1] >= threshold",
            "threshold": args.threshold,
            "target": args.target,
            "fail_closed": True,
        },
        "config": CONFIG,
        "training": {
            "dataset": args.dataset,
            "rows": int(len(df)),
            "date_start": str(df["time"].iloc[0]),
            "date_end": str(df["time"].iloc[-1]),
            "hyperparameters": {k: v for k, v in model.get_params().items()},
        },
        "walk_forward_reference": wf,
    }
    meta_path = args.out.replace(".onnx", ".meta.json")
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)
    print(f"Metadata: {meta_path}")
    if wf:
        print(f"Referensi WF (rf @ p>={args.threshold}): "
              f"E[r_net] {wf['expected_r_net']:+.3f} R over {wf['n_trades_wf']} trade, "
              f"{wf['folds_positive_expectancy']}/{wf['folds']} fold positif")


if __name__ == "__main__":
    main()
