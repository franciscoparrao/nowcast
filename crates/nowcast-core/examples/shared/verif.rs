//! Verification helpers shared by the backtest examples (`backtest`,
//! `antecedent_trigger`): Wilson intervals, month-level contingency, a
//! deterministic LCG, and the year-shuffle null. One copy on purpose (see
//! `docs/checklist-hermanas.md`): two examples testing against the same null
//! must not drift apart.
//!
//! This directory has no `main.rs`; consumers pull it in with
//! `#[path = "shared/verif.rs"] mod verif;`.

#![allow(dead_code)]

use std::collections::{BTreeMap, BTreeSet};

use nowcast_core::{Contingency, MonthKey};

/// Wilson 95 % interval for `k` successes in `n` trials.
pub fn wilson(k: u64, n: u64) -> (f64, f64) {
    if n == 0 {
        return (f64::NAN, f64::NAN);
    }
    let (z, p, n) = (1.96_f64, k as f64 / n as f64, n as f64);
    let denom = 1.0 + z * z / n;
    let centre = (p + z * z / (2.0 * n)) / denom;
    let half = z * (p * (1.0 - p) / n + z * z / (4.0 * n * n)).sqrt() / denom;
    (centre - half, centre + half)
}

/// Same counting as `nowcast_core::monthly_contingency`, on month-level sets.
pub fn month_contingency(
    present: &BTreeSet<MonthKey>,
    alerted: &BTreeSet<MonthKey>,
    observed: &BTreeSet<MonthKey>,
    tol: i32,
) -> Contingency {
    let shift = |(y, m): MonthKey, d: i32| -> MonthKey {
        let idx = y * 12 + (m as i32 - 1) + d;
        (idx.div_euclid(12), (idx.rem_euclid(12) + 1) as u32)
    };
    let alert_near = |mk: MonthKey| (-tol..=tol).any(|d| alerted.contains(&shift(mk, d)));
    let event_near = |mk: MonthKey| (-tol..=tol).any(|d| observed.contains(&shift(mk, d)));
    let mut c = Contingency::default();
    for &ev in observed {
        if alert_near(ev) {
            c.hits += 1;
        } else {
            c.misses += 1;
        }
    }
    for &mk in present {
        if event_near(mk) {
            continue;
        }
        if alerted.contains(&mk) {
            c.false_alarms += 1;
        } else {
            c.correct_negatives += 1;
        }
    }
    c
}

/// Deterministic LCG (Numerical Recipes constants) — no `rand` dependency.
pub struct Lcg(pub u64);
impl Lcg {
    pub fn next_u32(&mut self) -> u32 {
        self.0 = self.0.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
        (self.0 >> 32) as u32
    }
    pub fn below(&mut self, n: usize) -> usize {
        (self.next_u32() as usize) % n
    }
}

/// Year-shuffle null: alert-months of year y are reassigned to year perm(y),
/// keeping seasonality and within-year clustering. Returns (POD values, CSI
/// values) over `n_perm` permutations, sorted ascending.
pub fn year_shuffle(
    present: &BTreeSet<MonthKey>,
    alerted: &BTreeSet<MonthKey>,
    observed: &BTreeSet<MonthKey>,
    tol: i32,
    n_perm: usize,
    rng: &mut Lcg,
) -> (Vec<f64>, Vec<f64>) {
    let years: Vec<i32> = {
        let mut v: Vec<i32> = present.iter().map(|&(y, _)| y).collect();
        v.dedup();
        v
    };
    let (mut pods, mut csis) = (Vec::with_capacity(n_perm), Vec::with_capacity(n_perm));
    for _ in 0..n_perm {
        let mut perm = years.clone();
        for i in (1..perm.len()).rev() {
            let j = rng.below(i + 1);
            perm.swap(i, j);
        }
        let map: BTreeMap<i32, i32> = years.iter().copied().zip(perm).collect();
        let shuffled: BTreeSet<MonthKey> = alerted.iter().map(|&(y, m)| (map[&y], m)).collect();
        let cn = month_contingency(present, &shuffled, observed, tol);
        pods.push(cn.pod().unwrap_or(0.0));
        csis.push(cn.csi().unwrap_or(0.0));
    }
    pods.sort_by(|x, y| x.partial_cmp(y).unwrap());
    csis.sort_by(|x, y| x.partial_cmp(y).unwrap());
    (pods, csis)
}

/// Fraction of `sorted` values ≥ `x`.
pub fn frac_ge(sorted: &[f64], x: f64) -> f64 {
    sorted.iter().filter(|&&v| v >= x).count() as f64 / sorted.len() as f64
}
