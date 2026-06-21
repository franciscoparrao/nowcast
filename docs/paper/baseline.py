#!/usr/bin/env python3
"""Supervised baseline for the distributed Maipo backtest (review #4).

If a discriminative model TRAINED on the daily features (susceptibility + rainfall
statistics) also fails to separate event cell-months under honest cross-
validation, the signal is absent from the daily data, not merely from our I-D
trigger. Year-blocked GroupKFold avoids leakage from temporal autocorrelation.
Run from repo root with the system python (numpy/pandas/sklearn).
"""
import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import make_pipeline
from sklearn.model_selection import GroupKFold, cross_val_predict
from sklearn.metrics import roc_auc_score

NC = 270  # cells (15x18)

# --- label + ordering from the engine's cell-month table (same positives) ----
cm = np.genfromtxt("data/cellmonths.csv", delimiter=",", names=True)
mi = cm["month_idx"].astype(int)
cell = cm["cell"].astype(int)
y = cm["label"].astype(int)
year = 1979 + mi // 12  # months are contiguous from 1979-01

# --- susceptibility per cell ------------------------------------------------
g = np.genfromtxt("data/maipo_dist_grid.csv", delimiter=",", names=True)
susc_by_cell = np.zeros(NC)
susc_by_cell[g["cell"].astype(int)] = g["susceptibility"]

# --- daily precip -> per (month, cell) rainfall features --------------------
raw = np.genfromtxt("data/maipo_dist_pr.csv", delimiter=",", skip_header=1)
dates = np.genfromtxt("data/maipo_dist_pr.csv", delimiter=",", dtype=str,
                      skip_header=1, usecols=0)
P = raw[:, 1:]  # (days, cells)
dm = np.array([(int(s[:4]) - 1979) * 12 + (int(s[5:7]) - 1) for s in dates])
# 7-day rolling sum per cell
csum = np.cumsum(P, axis=0)
roll7 = csum.copy(); roll7[7:] = csum[7:] - csum[:-7]

n_months = dm.max() + 1
total = np.zeros((n_months, NC)); mx1 = np.zeros((n_months, NC)); mx7 = np.zeros((n_months, NC))
for k in range(n_months):
    rows = np.where(dm == k)[0]
    total[k] = P[rows].sum(0); mx1[k] = P[rows].max(0); mx7[k] = roll7[rows].max(0)
ante = np.vstack([np.zeros((1, NC)), total[:-1]])  # previous-month total

# --- assemble feature matrix aligned with cellmonths order ------------------
X = np.column_stack([
    susc_by_cell[cell],
    total[mi, cell], mx1[mi, cell], mx7[mi, cell], ante[mi, cell],
])
feat = ["susceptibility", "month_total", "max_1d", "max_7d", "antecedent"]
print(f"{X.shape[0]} cell-months, {int(y.sum())} positive, {len(np.unique(year))} year groups\n")

cv = GroupKFold(n_splits=5)
models = {
    "logistic regression": make_pipeline(StandardScaler(),
        LogisticRegression(class_weight="balanced", max_iter=2000)),
    "gradient boosting": HistGradientBoostingClassifier(
        class_weight="balanced", max_depth=3, learning_rate=0.05, max_iter=300),
}
print(f"{'baseline (year-blocked CV)':<26} {'CV AUC':>7}")
for name, mdl in models.items():
    proba = cross_val_predict(mdl, X, y, cv=cv, groups=year,
                              method="predict_proba")[:, 1]
    print(f"{name:<26} {roc_auc_score(y, proba):7.3f}")

# single strongest feature, for reference
print(f"\n{'single-feature AUC (in-sample)':<26}")
for j, f in enumerate(feat):
    print(f"  {f:<22} {roc_auc_score(y, X[:, j]):6.3f}")
