#!/usr/bin/env python3
"""Month-block bootstrap CIs for the distributed-backtest AUC (review #5).

Resampling months (not cell-months) with replacement respects the dominant
autocorrelation — one storm wets many cells in the same month — so the CI is not
artificially narrow. Reads data/cellmonths.csv (from the backtest_distributed
example). Run from repo root.
"""
import numpy as np

B = 2000
rng = np.random.default_rng(42)

d = np.genfromtxt("data/cellmonths.csv", delimiter=",", names=True)
m = d["month_idx"].astype(int)
y = d["label"].astype(int)
cols = {"lumped (basin-mean) x susc": d["s_lumped"],
        "distributed, susc = 1": d["s_dist1"],
        "distributed x real susc": d["s_distsusc"]}

# Row indices grouped by month, for fast block resampling.
months = np.unique(m)
rows_by_month = [np.where(m == mm)[0] for mm in months]


def avg_ranks(s):
    order = np.argsort(s, kind="mergesort")
    s_sorted = s[order]
    n = len(s)
    new_grp = np.r_[True, s_sorted[1:] != s_sorted[:-1]]
    grp = np.cumsum(new_grp) - 1
    pos = np.arange(n, dtype=float)
    meanpos = np.bincount(grp, weights=pos) / np.bincount(grp)
    avg_sorted = meanpos[grp] + 1.0
    r = np.empty(n)
    r[order] = avg_sorted
    return r


def auc(score, yy):
    npos = int(yy.sum()); nneg = len(yy) - npos
    if npos == 0 or nneg == 0:
        return np.nan
    r = avg_ranks(score)
    return (r[yy == 1].sum() - npos * (npos + 1) / 2.0) / (npos * nneg)


print(f"Month-block bootstrap, B={B}, {len(months)} months, {len(y)} cell-months, {int(y.sum())} positive\n")
print(f"{'configuration':<28} {'AUC':>6}  {'95% CI':>16}  {'P(AUC>0.5)':>11}")
for name, sc in cols.items():
    point = auc(sc, y)
    boot = np.empty(B)
    for b in range(B):
        samp = rng.integers(0, len(months), len(months))
        idx = np.concatenate([rows_by_month[i] for i in samp])
        boot[b] = auc(sc[idx], y[idx])
    boot = boot[~np.isnan(boot)]
    lo, hi = np.percentile(boot, [2.5, 97.5])
    p_gt = (boot > 0.5).mean()
    print(f"{name:<28} {point:6.3f}  [{lo:5.3f}, {hi:5.3f}]  {p_gt:11.2f}")
