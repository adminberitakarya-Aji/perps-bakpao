"""
Training ML walk-forward (Fase 2) -- pengganti docs/train_model.py yang
single 80/20 split. Pertanyaan yang dijawab: apakah probabilitas model
cukup tinggi untuk menembus break-even net-of-cost (~47-50% WR di BTC 15M)?

Desain:
- Walk-forward anchored dengan PURGE: HORIZON bar terakhir tiap train window
  dibuang (label-nya melihat 30 bar ke depan -> overlap dengan test = leakage).
- Model & hyperparameter sama dengan blueprint (RF 150, depth 6).
- Kalibrasi threshold: tabel expectancy net per bucket probabilitas &
  per threshold kandidat, agregat antar-fold. Aset beda -> threshold beda.

Jalankan:
    python -m src.ml.train_model --dataset data/btc_15m_ml_dataset.csv
    python -m src.ml.train_model --dataset data/btc_1h_ml_dataset.csv
    python -m src.ml.train_model --dataset docs/xauusd_ml_dataset.csv  (tanpa r_net -> evaluasi gross)
"""

import argparse
import sys

import numpy as np
import pandas as pd
from sklearn.ensemble import (
    HistGradientBoostingClassifier,
    RandomForestClassifier,
)
from sklearn.metrics import brier_score_loss, roc_auc_score

CORE_FEATURES = [
    "dist_to_ema_atr",
    "adx_main", "adx_pdi", "adx_mdi", "adx_di_diff",
    "rsi",
    "atr_normalized",
    "body_atr", "upper_shadow_atr", "lower_shadow_atr",
    "hour", "day_of_week",
    "signal_type",
]
# fitur tambahan (dipakai otomatis kalau ada di CSV)
EXTRA_FEATURES = ["vol_ratio_20", "vol_ratio_100", "funding_rate",
                  "rv_50", "dist_hi_500_atr", "dist_lo_500_atr"]
HORIZON = 30
RR = 1.5          # reward R saat TP
RISK = 1.0        # risiko R saat SL
THRESHOLDS = [0.45, 0.50, 0.55, 0.60, 0.65, 0.70]
N_FOLDS = 5
TEST_FRAC = 0.15  # tiap fold menguji 15% data berikutnya (anchored expanding)


def select_features(df: pd.DataFrame) -> list:
    return CORE_FEATURES + [c for c in EXTRA_FEATURES if c in df.columns]


def load_dataset(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    missing = [c for c in CORE_FEATURES + ["label_win"] if c not in df.columns]
    if missing:
        sys.exit(f"Kolom hilang di {path}: {missing}")
    if "r_net" not in df.columns:
        # fixture XAU lama: sintesis r_net gross dgn asumsi biaya 0 (label saja)
        df["r_net"] = np.where(df["label_win"] == 1, RR, -RISK)
        print("PERINGATAN: kolom r_net tidak ada -> evaluasi GROSS (asumsi biaya 0).")
    if "time" in df.columns:
        df = df.sort_values("time").reset_index(drop=True)
    return df


def breakeven_wr(cost_r: float) -> float:
    """p* agar E[R]=0 utk outcome biner TP/SL: p*(RR-cost) = (1-p)*(RISK+cost)."""
    return (RISK + cost_r) / (RR + RISK)


def make_model(name: str):
    if name == "histgb":
        # HistGradientBoosting: non-linear, menangani interaksi fitur lebih baik
        return HistGradientBoostingClassifier(
            max_iter=200, max_depth=4, learning_rate=0.08,
            min_samples_leaf=30, l2_regularization=1.0, random_state=42,
        )
    # "rf": hyperparameter identik docs/train_model.py (blueprint)
    return RandomForestClassifier(
        n_estimators=150, max_depth=6, min_samples_split=10,
        min_samples_leaf=5, random_state=42, class_weight="balanced",
        n_jobs=-1,
    )


def fold_bounds(n: int, n_folds: int, test_frac: float):
    """Anchored expanding: train selalu dari baris 0. Yield (train_end, test_end)."""
    test_size = int(n * test_frac)
    first_train = n - n_folds * test_size
    if first_train < 500:
        raise SystemExit(f"Data terlalu sedikit ({n} baris) utk {n_folds} fold.")
    for k in range(n_folds):
        train_end = first_train + k * test_size
        test_end = train_end + test_size
        yield train_end, test_end


def run_walk_forward(df: pd.DataFrame, label: str, model_name: str = "rf",
                     target: str = "label_win") -> pd.DataFrame:
    feats = select_features(df)
    X = df[feats].astype("float32")
    y = df[target].astype(int)
    r_col = "r_trail_net" if target == "label_trail" else "r_net"
    r_net = df[r_col].astype(float)
    # biaya implisit per trade, dari selisih R TP vs r_net (approx utk trail:
    # gross winner trailing < 1.5R -> sedikit OVERESTIMASI biaya, konservatif)
    cost_r = float((RR - r_net[y == 1]).mean())
    be = breakeven_wr(cost_r)
    print(f"\n{'=' * 72}\nDATASET: {label} | MODEL: {model_name}")
    print(f"{len(df)} kandidat | {len(feats)} fitur: {', '.join(feats)}")
    print(f"biaya/trade ~{cost_r:.3f} R | break-even WR net = {be * 100:.1f}%")
    print(f"Baseline semua kandidat: mean r_net {r_net.mean():+.3f} R | "
          f"WR net {(r_net > 0).mean() * 100:.1f}%")

    fold_results = []
    for k, (train_end, test_end) in enumerate(fold_bounds(len(df), N_FOLDS, TEST_FRAC), 1):
        tr_end_purged = train_end - HORIZON  # purge: buang label yang bocor ke test
        X_tr, y_tr = X.iloc[:tr_end_purged], y.iloc[:tr_end_purged]
        X_te = X.iloc[train_end:test_end]
        y_te = y.iloc[train_end:test_end]
        r_te = r_net.iloc[train_end:test_end]

        model = make_model(model_name)
        model.fit(X_tr, y_tr)
        proba = model.predict_proba(X_te)[:, 1]

        auc = roc_auc_score(y_te, proba)
        brier = brier_score_loss(y_te, proba)
        print(f"\n--- FOLD {k}: train [{0}:{tr_end_purged}] ({len(X_tr)}), "
              f"test [{train_end}:{test_end}] ({len(X_te)}) ---")
        print(f"    AUC={auc:.3f} | Brier={brier:.3f} | base rate test={y_te.mean() * 100:.1f}%")

        for thr in THRESHOLDS:
            mask = proba >= thr
            n = int(mask.sum())
            row = {"model": model_name, "fold": k, "threshold": thr, "n": n,
                   "auc": round(auc, 3)}
            if n >= 20:
                rr = r_te[mask]
                row.update({
                    "wr_net": round(float((rr > 0).mean() * 100), 1),
                    "exp_net": round(float(rr.mean()), 3),
                    "wr_gross": round(float(y_te[mask].mean() * 100), 1),
                })
            fold_results.append(row)
            if n:
                exp_s = f"{row.get('exp_net', float('nan')):+.3f}"
                wr_s = f"{row.get('wr_net', float('nan')):.1f}"
                edge = "BE+ " if row.get("exp_net", -9) > 0 else "    "
                print(f"    p>={thr:.2f}: n={n:5d} | WR net {wr_s:>5s}% (BE {be * 100:.1f}%) "
                      f"| E[r_net] {exp_s} R {edge}")
            else:
                print(f"    p>={thr:.2f}: n=    0 | (tidak ada kandidat terpilih)")

    return pd.DataFrame(fold_results)


def summarize(results: pd.DataFrame, cost_r: float) -> None:
    be = breakeven_wr(cost_r) * 100
    best_overall = None
    for model_name, g_model in results.groupby("model"):
        print(f"\n{'=' * 72}\nAGREGAT ANTAR-FOLD (threshold kalibrasi) -- model: {model_name}")
        print(f"{'thr':>5} | {'total n':>7} | {'folds aktif':>11} | {'E[r_net]':>9} | "
              f"{'WR net':>7} | {'BE':>5} | {'fold BE+':>8} | {'folds E>0':>9}")
        best = None
        for thr, g in g_model.groupby("threshold"):
            sel = g[g["n"] >= 20]
            n_tot = int(sel["n"].sum())
            if n_tot == 0:
                print(f"{thr:>5.2f} | {'-':>7} | 0 | - | - | - | - | -")
                continue
            exp_w = (sel["exp_net"] * sel["n"]).sum() / n_tot
            wr_w = (sel["wr_net"] * sel["n"]).sum() / n_tot
            folds_pos = int((sel["exp_net"] > 0).sum())
            folds_be = int((sel["wr_net"] >= be).sum())
            print(f"{thr:>5.2f} | {n_tot:>7d} | {len(sel):>11d} | {exp_w:>+9.3f} | "
                  f"{wr_w:>6.1f}% | {be:>4.1f}% | {folds_be:>3d}/{len(sel)}   | {folds_pos:>3d}/{len(sel)}")
            if best is None or exp_w > best[1]:
                best = (thr, exp_w, n_tot)
        if best and best[1] > 0:
            print(f"\nKESIMPULAN [{model_name}]: threshold terbaik p>={best[0]:.2f} -> "
                  f"E[r_net] {best[1]:+.3f} R over {best[2]} trade -> MENEMBUS break-even.")
        elif best:
            print(f"\nKESIMPULAN [{model_name}]: threshold terbaik p>={best[0]:.2f} masih "
                  f"{best[1]:+.3f} R -> BELUM menembus break-even net-of-cost.")
        if best and (best_overall is None or best[1] > best_overall[1][1]):
            best_overall = (model_name, best)
    return best_overall


def main():
    global N_FOLDS
    ap = argparse.ArgumentParser(description="Training ML walk-forward + kalibrasi threshold")
    ap.add_argument("--dataset", default="data/btc_15m_ml_dataset.csv")
    ap.add_argument("--folds", type=int, default=N_FOLDS)
    ap.add_argument("--model", choices=["rf", "histgb", "both"], default="both")
    ap.add_argument("--target", choices=["label_win", "label_trail"],
                    default="label_win",
                    help="label_trail = target simulasi trailing engine")
    args = ap.parse_args()
    N_FOLDS = args.folds

    df = load_dataset(args.dataset)
    models = ["rf", "histgb"] if args.model == "both" else [args.model]

    # biaya implisit dihitung ulang di sini untuk ringkasan
    r_col = "r_trail_net" if args.target == "label_trail" else "r_net"
    r_tp = df.loc[df[args.target] == 1, r_col]
    cost_r = float((RR - r_tp).mean()) if len(r_tp) else 0.0

    all_results = []
    for m in models:
        all_results.append(run_walk_forward(df, args.dataset, m, args.target))
    results = pd.concat(all_results, ignore_index=True)
    suffix = f"_{args.target.replace('label_', '')}" if args.target != "label_win" else ""
    results.to_csv(args.dataset.replace(".csv", f"_wf{suffix}_results.csv"), index=False)
    best = summarize(results, cost_r)
    if best:
        print(f"\n>>> PEMENANG: model {best[0]} @ p>={best[1][0]:.2f} "
              f"(E[r_net] {best[1][1]:+.3f} R, n={best[1][2]})")



if __name__ == "__main__":
    main()
