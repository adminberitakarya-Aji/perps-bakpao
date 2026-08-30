"""
Script Training Machine Learning untuk EA XAUUSD (Fase 2)
Memproses dataset fitur -> Melatih Classifier -> Mengekspor ke model ONNX
"""

import os
import sys
import numpy as np
import pandas as pd
from sklearn.model_selection import TimeSeriesSplit
from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier
from sklearn.metrics import classification_report, roc_auc_score, confusion_matrix
from skl2onnx import convert_sklearn
from skl2onnx.common.data_types import FloatTensorType

# Fitur yang digunakan untuk inferensi
FEATURE_COLS = [
    "dist_to_ema_atr",
    "adx_main",
    "adx_pdi",
    "adx_mdi",
    "adx_di_diff",
    "rsi",
    "atr_normalized",
    "body_atr",
    "upper_shadow_atr",
    "lower_shadow_atr",
    "hour",
    "day_of_week",
    "signal_type"
]

TARGET_COL = "label_win"

def load_data(csv_path: str):
    if not os.path.exists(csv_path):
        print(f"File {csv_path} tidak ditemukan!")
        print("Silakan jalankan script MQL5 Export_ML_Dataset.mq5 terlebih dahulu.")
        sys.exit(1)
    
    df = pd.read_csv(csv_path)
    print(f"Dataset berhasil dimuat: {df.shape[0]} baris, {df.shape[1]} kolom.")
    return df

def train_and_export(df: pd.DataFrame, output_onnx: str = "model_xau.onnx"):
    X = df[FEATURE_COLS].astype(np.float32)
    y = df[TARGET_COL].astype(int)

    # Time-Series Split (80% Train, 20% Test)
    split_idx = int(len(df) * 0.8)
    X_train, X_test = X.iloc[:split_idx], X.iloc[split_idx:]
    y_train, y_test = y.iloc[:split_idx], y.iloc[split_idx:]

    print(f"Data Training: {len(X_train)} sampel | Data Testing: {len(X_test)} sampel")
    print(f"Distribusi Win pada Train: {y_train.mean()*100:.1f}% | Test: {y_test.mean()*100:.1f}%")

    # Inisialisasi Model Classifier
    model = RandomForestClassifier(
        n_estimators=150,
        max_depth=6,
        min_samples_split=10,
        min_samples_leaf=5,
        random_state=42,
        class_weight="balanced"
    )

    print("\nMelatih model...")
    model.fit(X_train, y_train)

    # Evaluasi
    y_pred = model.predict(X_test)
    y_proba = model.predict_proba(X_test)[:, 1]

    print("\n=== HASIL EVALUASI MODEL PADA DATA TEST ===")
    print(classification_report(y_test, y_pred, target_names=["Loss", "Win"]))
    print(f"ROC AUC Score: {roc_auc_score(y_test, y_proba):.4f}")
    print("\nConfusion Matrix:")
    print(confusion_matrix(y_test, y_pred))

    # Feature Importance
    importances = pd.Series(model.feature_importances_, index=FEATURE_COLS).sort_values(ascending=False)
    print("\n=== TOP FEATURE IMPORTANCE ===")
    for feat, imp in importances.items():
        print(f"  - {feat:20s}: {imp*100:.2f}%")

    # Ekspor ke ONNX
    print(f"\nMengekspor model ke format ONNX: {output_onnx} ...")
    initial_type = [('float_input', FloatTensorType([None, len(FEATURE_COLS)]))]
    options = {id(model): {'zipmap': False}}
    onnx_model = convert_sklearn(model, initial_types=initial_type, target_opset=12, options=options)

    with open(output_onnx, "wb") as f:
        f.write(onnx_model.SerializeToString())

    print(f"Sukses! Model ONNX tersimpan di: {os.path.abspath(output_onnx)}")
    print(f"Jumlah Fitur Input: {len(FEATURE_COLS)}")
    print("Daftar Urutan Fitur Input:")
    for i, col in enumerate(FEATURE_COLS):
        print(f"  [{i}] {col}")

if __name__ == "__main__":
    csv_file = sys.argv[1] if len(sys.argv) > 1 else "xauusd_ml_dataset.csv"
    data = load_data(csv_file)
    train_and_export(data)
