#!/usr/bin/env python3
"""Supervised baseline for the distributed Maipo backtest (review #4).

If a discriminative model TRAINED on the daily features (susceptibility + rainfall
statistics) also fails to separate event cell-months under honest cross-
validation, the signal is absent from the daily data, not merely from our I-D
trigger. Year-blocked folds avoid leakage from temporal autocorrelation.
Run from repo root with the system python (numpy/pandas/sklearn).
"""
import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import make_pipeline
from sklearn.model_selection import cross_val_predict
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

# --- folds: fixed by construction, not by a sort --------------------------
# Every year holds exactly 270 x 12 = 3240 cell-months, so GroupKFold's
# size-balancing faces a 38-way tie and the split was decided by numpy's
# argsort tie-breaking: numpy 1.26.4 gave CV AUC 0.560 / 0.521 and numpy 2.5.3
# gave 0.583 / 0.536 with the same sklearn 1.8.0 and scipy 1.17.1 (2026-11-04).
# The primary split is now five blocks of consecutive years; the spread over
# N_REP random year-to-fold assignments (Python's `random`, whose stream does
# not depend on numpy) is reported beside it as the fold-assignment range.
import random
K, N_REP, SEED = 5, 50, 20261105
years = np.unique(year)


def folds_from(assign):
    """assign: year -> fold. Returns sklearn-style (train, test) index pairs."""
    f = np.array([assign[v] for v in year])
    return [(np.where(f != k)[0], np.where(f == k)[0]) for k in range(K)]


blocks = {v: i * K // len(years) for i, v in enumerate(years)}
rng = random.Random(SEED)
reps = []
for _ in range(N_REP):
    ys = [int(v) for v in years]
    rng.shuffle(ys)
    reps.append({v: i % K for i, v in enumerate(ys)})

models = {
    "logistic regression": make_pipeline(StandardScaler(),
        LogisticRegression(class_weight="balanced", max_iter=2000)),
    # random_state fixes the internal early-stopping split (on for >10k rows)
    "gradient boosting": HistGradientBoostingClassifier(
        class_weight="balanced", max_depth=3, learning_rate=0.05, max_iter=300,
        random_state=0),
}


def cv_auc(mdl, assign):
    proba = cross_val_predict(mdl, X, y, cv=folds_from(assign),
                              method="predict_proba")[:, 1]
    return roc_auc_score(y, proba)


print(f"{'baseline (year-blocked CV)':<26} {'blocks':>7}   "
      f"{'random year-to-fold assignments (n=' + str(N_REP) + ')':>44}")
print(f"{'':<26} {'':>7}   {'min':>6} {'p2.5':>6} {'median':>6} {'p97.5':>6} {'max':>6}")
for name, mdl in models.items():
    a0 = cv_auc(mdl, blocks)
    r = np.array([cv_auc(mdl, a) for a in reps])
    q = np.percentile(r, [0, 2.5, 50, 97.5, 100])
    print(f"{name:<26} {a0:7.3f}   " + " ".join(f"{v:6.3f}" for v in q))

# single strongest feature, for reference
print(f"\n{'single-feature AUC (in-sample)':<26}")
for j, f in enumerate(feat):
    print(f"  {f:<22} {roc_auc_score(y, X[:, j]):6.3f}")
