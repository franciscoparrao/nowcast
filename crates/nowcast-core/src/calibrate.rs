//! Probability calibration and reliability.
//!
//! The engine's hazard is a bounded **index** (`susceptibility × trigger`), not a
//! calibrated probability of an event. This module turns it into one and quantifies
//! how trustworthy that number is, with no external dependencies.
//!
//! * [`Calibrator`] fits a monotone map *raw index → calibrated probability* by
//!   **isotonic regression** (pool-adjacent-violators). Isotonic is non-parametric
//!   and shape-free: it only assumes that a higher index means a higher (or equal)
//!   event probability, which the hazard construction guarantees.
//! * [`reliability`] builds a **reliability diagram**: per-bin predicted vs observed
//!   frequency, each with a **Wilson 95 % interval** — the per-bin uncertainty that
//!   comes from finite sample counts. It also reports the **Brier score**, its skill
//!   against climatology, and the expected calibration error (ECE).
//!
//! Fit on held-out backtest pairs `(hazard_index, event_occurred)`; then
//! [`Calibrator::probability`] converts any future index into a calibrated
//! probability.

use crate::error::{Error, Result};

const Z95: f64 = 1.959_963_984_540_054; // 97.5th percentile of the standard normal

/// A monotone calibration map fitted by isotonic regression.
///
/// With the `serde` feature the fitted map serializes — fit offline on backtest
/// pairs, persist to JSON, and load it in an operational `watch`. Deserialized
/// data is **not** trusted: call [`validate`](Self::validate) after loading
/// untrusted (e.g. hand-edited) JSON before using it.
#[derive(Debug, Clone)]
#[cfg_attr(feature = "serde", derive(serde::Serialize, serde::Deserialize))]
pub struct Calibrator {
    /// Block mean scores (ascending).
    xs: Vec<f64>,
    /// Calibrated probabilities per block (non-decreasing), in `[0, 1]`.
    ys: Vec<f64>,
}

impl Calibrator {
    /// Fit *score → probability* by isotonic regression (pool-adjacent-violators)
    /// on paired raw `scores` and binary `outcomes`. Errors on length mismatch or
    /// empty input.
    pub fn fit_isotonic(scores: &[f64], outcomes: &[bool]) -> Result<Self> {
        if scores.len() != outcomes.len() {
            return Err(Error::InvalidParameter {
                name: "outcomes",
                reason: format!("{} scores but {} outcomes", scores.len(), outcomes.len()),
            });
        }
        if scores.is_empty() {
            return Err(Error::InvalidParameter {
                name: "scores",
                reason: "need at least one (score, outcome) pair".into(),
            });
        }
        if scores.iter().any(|s| !s.is_finite()) {
            return Err(Error::InvalidParameter {
                name: "scores",
                reason: "must all be finite".into(),
            });
        }
        let mut order: Vec<usize> = (0..scores.len()).collect();
        order.sort_by(|&a, &b| scores[a].partial_cmp(&scores[b]).unwrap());

        // Pool-adjacent-violators: merge while the previous block's mean exceeds
        // the current one's, restoring a non-decreasing sequence of block means.
        struct Block {
            sum_x: f64,
            sum_y: f64,
            n: usize,
        }
        let mut blocks: Vec<Block> = Vec::new();
        for &i in &order {
            let mut b = Block {
                sum_x: scores[i],
                sum_y: if outcomes[i] { 1.0 } else { 0.0 },
                n: 1,
            };
            while let Some(prev) = blocks.last() {
                if prev.sum_y / prev.n as f64 > b.sum_y / b.n as f64 {
                    let p = blocks.pop().unwrap();
                    b.sum_x += p.sum_x;
                    b.sum_y += p.sum_y;
                    b.n += p.n;
                } else {
                    break;
                }
            }
            blocks.push(b);
        }
        let xs = blocks.iter().map(|b| b.sum_x / b.n as f64).collect();
        let ys = blocks.iter().map(|b| (b.sum_y / b.n as f64).clamp(0.0, 1.0)).collect();
        Ok(Self { xs, ys })
    }

    /// Calibrated probability for a raw `score` (monotone linear interpolation
    /// between block means; clamped to the fitted range outside it).
    pub fn probability(&self, score: f64) -> f64 {
        let (xs, ys) = (&self.xs, &self.ys);
        let last = xs.len() - 1;
        if score <= xs[0] {
            return ys[0];
        }
        if score >= xs[last] {
            return ys[last];
        }
        match xs.binary_search_by(|v| v.partial_cmp(&score).unwrap()) {
            Ok(k) => ys[k],
            Err(k) => {
                let (x0, x1, y0, y1) = (xs[k - 1], xs[k], ys[k - 1], ys[k]);
                if x1 == x0 {
                    y1.max(y0)
                } else {
                    y0 + (y1 - y0) * (score - x0) / (x1 - x0)
                }
            }
        }
    }

    /// Calibrate a slice of raw scores into probabilities.
    pub fn calibrate(&self, scores: &[f64]) -> Vec<f64> {
        scores.iter().map(|&s| self.probability(s)).collect()
    }

    /// Checks the invariants [`fit_isotonic`](Self::fit_isotonic) guarantees but
    /// that a deserialized `Calibrator` — e.g. a hand-edited or corrupted
    /// `--calibrator` JSON file — is not guaranteed to satisfy: non-empty,
    /// `xs`/`ys` the same length, both finite, `ys` within `[0, 1]`, and both
    /// non-decreasing (so [`probability`](Self::probability)'s binary search and
    /// interpolation stay in range instead of panicking or extrapolating
    /// nonsense). Call this right after deserializing untrusted input.
    pub fn validate(&self) -> Result<()> {
        if self.xs.len() != self.ys.len() {
            return Err(Error::InvalidParameter {
                name: "calibrator",
                reason: format!(
                    "{} score blocks but {} probability blocks",
                    self.xs.len(),
                    self.ys.len()
                ),
            });
        }
        if self.xs.is_empty() {
            return Err(Error::InvalidParameter {
                name: "calibrator",
                reason: "must have at least one calibration block".into(),
            });
        }
        if self.xs.iter().any(|x| !x.is_finite()) || self.ys.iter().any(|y| !y.is_finite()) {
            return Err(Error::InvalidParameter {
                name: "calibrator",
                reason: "scores and probabilities must be finite".into(),
            });
        }
        if self.ys.iter().any(|y| !(0.0..=1.0).contains(y)) {
            return Err(Error::InvalidParameter {
                name: "calibrator",
                reason: "probabilities must be within [0, 1]".into(),
            });
        }
        if self.xs.windows(2).any(|w| w[0] > w[1]) {
            return Err(Error::InvalidParameter {
                name: "calibrator",
                reason: "scores must be non-decreasing".into(),
            });
        }
        if self.ys.windows(2).any(|w| w[0] > w[1]) {
            return Err(Error::InvalidParameter {
                name: "calibrator",
                reason: "probabilities must be non-decreasing (isotonic)".into(),
            });
        }
        Ok(())
    }
}

/// One bin of a reliability diagram.
#[derive(Debug, Clone)]
#[cfg_attr(feature = "serde", derive(serde::Serialize, serde::Deserialize))]
pub struct ReliabilityBin {
    /// Mean predicted probability of the bin's members.
    pub p_pred_mean: f64,
    /// Observed event frequency in the bin.
    pub p_obs: f64,
    /// Number of predictions in the bin.
    pub n: usize,
    /// Wilson 95 % lower bound on the observed frequency.
    pub ci_low: f64,
    /// Wilson 95 % upper bound on the observed frequency.
    pub ci_high: f64,
}

/// Reliability of probabilistic predictions against binary outcomes.
#[derive(Debug, Clone)]
#[cfg_attr(feature = "serde", derive(serde::Serialize, serde::Deserialize))]
pub struct Reliability {
    /// Brier score (mean squared error of the probabilities); lower is better.
    pub brier: f64,
    /// Brier skill score against the base-rate climatology; `> 0` beats it.
    pub brier_skill: f64,
    /// Expected calibration error: sample-weighted mean `|p_obs − p_pred|`.
    pub ece: f64,
    /// Event base rate (climatology).
    pub base_rate: f64,
    /// Non-empty reliability bins, ascending in predicted probability.
    pub bins: Vec<ReliabilityBin>,
}

/// Brier score: mean squared error between probabilities and `{0,1}` outcomes.
/// Errors on length mismatch or empty input — a silently truncated `zip` here
/// would return a numerically wrong score for the engine's calibration claim.
pub fn brier_score(preds: &[f64], outcomes: &[bool]) -> Result<f64> {
    if preds.len() != outcomes.len() {
        return Err(Error::InvalidParameter {
            name: "outcomes",
            reason: format!("{} preds but {} outcomes", preds.len(), outcomes.len()),
        });
    }
    if preds.is_empty() {
        return Err(Error::InvalidParameter {
            name: "preds",
            reason: "need at least one (prediction, outcome) pair".into(),
        });
    }
    if preds.iter().any(|p| !p.is_finite()) {
        return Err(Error::InvalidParameter {
            name: "preds",
            reason: "must all be finite".into(),
        });
    }
    let s: f64 = preds
        .iter()
        .zip(outcomes)
        .map(|(&p, &o)| {
            let y = if o { 1.0 } else { 0.0 };
            (p - y) * (p - y)
        })
        .sum();
    Ok(s / preds.len() as f64)
}

/// Wilson score interval for `k` successes in `n` trials at `z` standard normals.
fn wilson(k: usize, n: usize, z: f64) -> (f64, f64) {
    if n == 0 {
        return (0.0, 1.0);
    }
    let nf = n as f64;
    let p = k as f64 / nf;
    let denom = 1.0 + z * z / nf;
    let center = (p + z * z / (2.0 * nf)) / denom;
    let half = z * (p * (1.0 - p) / nf + z * z / (4.0 * nf * nf)).sqrt() / denom;
    ((center - half).max(0.0), (center + half).min(1.0))
}

/// Build a reliability diagram with `n_bins` equal-width bins over `[0, 1]`,
/// plus Brier score, Brier skill score and ECE. Errors on length mismatch,
/// empty input or `n_bins == 0`.
pub fn reliability(preds: &[f64], outcomes: &[bool], n_bins: usize) -> Result<Reliability> {
    if preds.len() != outcomes.len() {
        return Err(Error::InvalidParameter {
            name: "outcomes",
            reason: format!("{} preds but {} outcomes", preds.len(), outcomes.len()),
        });
    }
    if preds.is_empty() {
        return Err(Error::InvalidParameter {
            name: "preds",
            reason: "need at least one (prediction, outcome) pair".into(),
        });
    }
    if n_bins == 0 {
        return Err(Error::InvalidParameter {
            name: "n_bins",
            reason: "must be >= 1".into(),
        });
    }
    if preds.iter().any(|p| !p.is_finite()) {
        return Err(Error::InvalidParameter {
            name: "preds",
            reason: "must all be finite".into(),
        });
    }
    let n = preds.len();
    let base_rate = outcomes.iter().filter(|&&o| o).count() as f64 / n as f64;

    let mut sum_pred = vec![0.0; n_bins];
    let mut hits = vec![0usize; n_bins];
    let mut count = vec![0usize; n_bins];
    for (&p, &o) in preds.iter().zip(outcomes) {
        let pc = p.clamp(0.0, 1.0);
        let mut b = (pc * n_bins as f64) as usize;
        if b >= n_bins {
            b = n_bins - 1; // p == 1.0 lands in the last bin
        }
        sum_pred[b] += pc;
        count[b] += 1;
        if o {
            hits[b] += 1;
        }
    }

    let mut bins = Vec::new();
    let mut ece = 0.0;
    for b in 0..n_bins {
        if count[b] == 0 {
            continue;
        }
        let p_pred_mean = sum_pred[b] / count[b] as f64;
        let p_obs = hits[b] as f64 / count[b] as f64;
        let (ci_low, ci_high) = wilson(hits[b], count[b], Z95);
        ece += count[b] as f64 / n as f64 * (p_obs - p_pred_mean).abs();
        bins.push(ReliabilityBin { p_pred_mean, p_obs, n: count[b], ci_low, ci_high });
    }

    let brier = brier_score(preds, outcomes)?; // lengths validated above
    let brier_ref = base_rate * (1.0 - base_rate); // climatology Brier
    let brier_skill = if brier_ref > 0.0 { 1.0 - brier / brier_ref } else { f64::NAN };

    Ok(Reliability { brier, brier_skill, ece, base_rate, bins })
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Deterministic LCG → uniform in [0,1), so tests need no rng dependency.
    struct Lcg(u64);
    impl Lcg {
        fn unit(&mut self) -> f64 {
            self.0 = self.0.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
            (self.0 >> 11) as f64 / (1u64 << 53) as f64
        }
    }

    #[test]
    fn isotonic_is_monotone_and_recovers_a_step() {
        // True probability: 0 below 0.5, 1 above — a clean step.
        let mut rng = Lcg(1);
        let (mut scores, mut outcomes) = (Vec::new(), Vec::new());
        for _ in 0..4000 {
            let s = rng.unit();
            scores.push(s);
            outcomes.push(s > 0.5);
        }
        let cal = Calibrator::fit_isotonic(&scores, &outcomes).unwrap();
        // Monotone non-decreasing.
        let grid: Vec<f64> = (0..=20).map(|i| i as f64 / 20.0).collect();
        let probs: Vec<f64> = grid.iter().map(|&s| cal.probability(s)).collect();
        for w in probs.windows(2) {
            assert!(w[1] >= w[0] - 1e-9, "calibrator must be monotone");
        }
        assert!(cal.probability(0.2) < 0.1, "low scores → low prob");
        assert!(cal.probability(0.8) > 0.9, "high scores → high prob");
    }

    #[test]
    fn calibration_improves_brier_on_miscalibrated_scores() {
        // Outcomes follow true_p(s) = s, but the model reports a squashed score
        // s^2 (systematically under-confident) — miscalibrated by construction.
        let mut rng = Lcg(7);
        let (mut raw, mut outcomes) = (Vec::new(), Vec::new());
        for _ in 0..8000 {
            let s = rng.unit();
            raw.push(s * s);
            outcomes.push(rng.unit() < s);
        }
        let before = brier_score(&raw, &outcomes).unwrap();
        let cal = Calibrator::fit_isotonic(&raw, &outcomes).unwrap();
        let after = brier_score(&cal.calibrate(&raw), &outcomes).unwrap();
        assert!(after <= before + 1e-12, "calibration should not worsen Brier");
        assert!(after < before, "calibration should improve a miscalibrated model");
    }

    #[test]
    fn reliability_wilson_and_ece_are_sane() {
        // Perfectly calibrated predictions: outcome ~ Bernoulli(p).
        let mut rng = Lcg(3);
        let (mut preds, mut outcomes) = (Vec::new(), Vec::new());
        for _ in 0..20000 {
            let p = rng.unit();
            preds.push(p);
            outcomes.push(rng.unit() < p);
        }
        let r = reliability(&preds, &outcomes, 10).unwrap();
        assert!(r.ece < 0.05, "well-calibrated predictions have small ECE, got {}", r.ece);
        for b in &r.bins {
            assert!(b.ci_low <= b.p_obs && b.p_obs <= b.ci_high);
            assert!(b.ci_low >= 0.0 && b.ci_high <= 1.0);
        }
        // Brier skill positive: informative predictions beat climatology.
        assert!(r.brier_skill > 0.0, "skill {} should beat climatology", r.brier_skill);
    }

    #[test]
    fn rejects_bad_input() {
        assert!(Calibrator::fit_isotonic(&[0.1], &[true, false]).is_err());
        assert!(Calibrator::fit_isotonic(&[], &[]).is_err());
        assert!(reliability(&[0.5], &[true], 0).is_err());
        // brier_score must refuse mismatched lengths (a silent zip-truncation
        // here returned a wrong score before) and empty input.
        assert!(brier_score(&[0.5, 0.5], &[true]).is_err());
        assert!(brier_score(&[], &[]).is_err());
        assert!((brier_score(&[1.0], &[true]).unwrap()).abs() < 1e-12);
    }

    #[test]
    fn rejects_non_finite_input() {
        assert!(Calibrator::fit_isotonic(&[0.1, f64::NAN], &[true, false]).is_err());
        assert!(Calibrator::fit_isotonic(&[0.1, f64::INFINITY], &[true, false]).is_err());
        assert!(brier_score(&[0.5, f64::NAN], &[true, false]).is_err());
        assert!(reliability(&[0.5, f64::NAN], &[true, false], 5).is_err());
    }

    #[test]
    fn validate_catches_a_corrupted_or_hand_edited_calibrator() {
        // A legitimately fitted calibrator always validates.
        let good = Calibrator { xs: vec![0.1, 0.5, 0.9], ys: vec![0.0, 0.5, 1.0] };
        assert!(good.validate().is_ok());

        // Empty: the underflow that used to panic in `probability` (xs.len() - 1).
        assert!(Calibrator { xs: vec![], ys: vec![] }.validate().is_err());
        // Length mismatch.
        assert!(Calibrator { xs: vec![0.1, 0.5], ys: vec![0.0] }.validate().is_err());
        // Probability out of [0, 1]: the panic in `calibrate_field`'s HazardField::new.
        assert!(Calibrator { xs: vec![0.1, 0.9], ys: vec![1.5, 2.0] }.validate().is_err());
        // Non-finite.
        assert!(Calibrator { xs: vec![0.1, f64::NAN], ys: vec![0.0, 1.0] }.validate().is_err());
        // Not monotone (not actually isotonic).
        assert!(Calibrator { xs: vec![0.1, 0.9], ys: vec![0.8, 0.2] }.validate().is_err());
    }
}
