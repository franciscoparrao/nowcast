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
        order.sort_by(|&a, &b| scores[a].total_cmp(&scores[b]));

        // Secondary pooling first: samples with the *same* score collapse into
        // one initial block. Isotonic regression is a function of x — equal
        // scores must receive equal fitted values — and without this step the
        // fit depended on the input ORDER of tied samples (a real hazard index
        // is massively tied: every dry cell-step shares susc·factor(0)).
        let mut groups: Vec<(f64, f64, usize)> = Vec::new(); // (x, sum_y, n)
        for &i in &order {
            let y = if outcomes[i] { 1.0 } else { 0.0 };
            match groups.last_mut() {
                Some((x, sum_y, n)) if *x == scores[i] => {
                    *sum_y += y;
                    *n += 1;
                }
                _ => groups.push((scores[i], y, 1)),
            }
        }

        // Pool-adjacent-violators: merge while the previous block's mean exceeds
        // the current one's, restoring a non-decreasing sequence of block means.
        struct Block {
            sum_x: f64,
            sum_y: f64,
            n: usize,
        }
        let mut blocks: Vec<Block> = Vec::new();
        for (x, sum_y, n) in groups {
            let mut b = Block { sum_x: x * n as f64, sum_y, n };
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
    /// between block means; clamped to the fitted range outside it). Errors on
    /// a non-finite score — a `NaN` here used to panic the binary search, the
    /// same unguarded sibling boundary that [`fit_isotonic`](Self::fit_isotonic)
    /// already closes on the fitting side.
    pub fn probability(&self, score: f64) -> Result<f64> {
        if !score.is_finite() {
            return Err(Error::InvalidParameter {
                name: "score",
                reason: format!("must be finite, got {score}"),
            });
        }
        Ok(self.probability_finite(score))
    }

    /// Interpolation core; `score` must be finite (callers validate).
    fn probability_finite(&self, score: f64) -> f64 {
        let (xs, ys) = (&self.xs, &self.ys);
        let last = xs.len() - 1;
        if score <= xs[0] {
            return ys[0];
        }
        if score >= xs[last] {
            return ys[last];
        }
        match xs.binary_search_by(|v| v.total_cmp(&score)) {
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

    /// Calibrate a slice of raw scores into probabilities. Errors if any score
    /// is non-finite (validated once up front, then interpolated).
    pub fn calibrate(&self, scores: &[f64]) -> Result<Vec<f64>> {
        if let Some(s) = scores.iter().find(|s| !s.is_finite()) {
            return Err(Error::InvalidParameter {
                name: "scores",
                reason: format!("must all be finite, got {s}"),
            });
        }
        Ok(scores.iter().map(|&s| self.probability_finite(s)).collect())
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
    if let Some(p) = preds.iter().find(|p| !(0.0..=1.0).contains(*p)) {
        return Err(Error::InvalidParameter {
            name: "preds",
            reason: format!("must all be probabilities in [0, 1], got {p}"),
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
/// empty input, `n_bins == 0`, or predictions outside `[0, 1]` — the diagram
/// used to bin a clamped copy while the Brier score used the raw value, so an
/// out-of-range "probability" produced two silently inconsistent readings.
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
    if let Some(p) = preds.iter().find(|p| !(0.0..=1.0).contains(*p)) {
        return Err(Error::InvalidParameter {
            name: "preds",
            reason: format!("must all be probabilities in [0, 1], got {p}"),
        });
    }
    let n = preds.len();
    let base_rate = outcomes.iter().filter(|&&o| o).count() as f64 / n as f64;

    let mut sum_pred = vec![0.0; n_bins];
    let mut hits = vec![0usize; n_bins];
    let mut count = vec![0usize; n_bins];
    for (&p, &o) in preds.iter().zip(outcomes) {
        let mut b = (p * n_bins as f64) as usize;
        if b >= n_bins {
            b = n_bins - 1; // p == 1.0 lands in the last bin
        }
        sum_pred[b] += p;
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

/// Aligned `(score, outcome)` pairs from two 0-based CSV columns of the same
/// text — the parser behind `nowcast calibrate`.
///
/// Unlike two independent [`csv_column`](crate::csv_column) passes (which skip
/// unparseable lines *per column* and can silently pair values from different
/// rows when parse failures cross), this walks the file **row by row**: a line
/// where both fields are missing/unparseable/non-finite is skipped (tolerates a
/// header), but a line where exactly one of the two parses is a hard error —
/// that is the misalignment that would poison the fitted calibrator.
pub fn csv_pairs(text: &str, col_a: usize, col_b: usize) -> Result<Vec<(f64, f64)>> {
    let parse = |line: &str, col: usize| -> Option<f64> {
        line.split(',')
            .nth(col)
            .and_then(|f| f.trim().parse::<f64>().ok())
            .filter(|v| v.is_finite())
    };
    let mut pairs = Vec::new();
    for (i, line) in text.lines().enumerate() {
        match (parse(line, col_a), parse(line, col_b)) {
            (Some(a), Some(b)) => pairs.push((a, b)),
            (None, None) => {} // header / blank / fully unparseable line
            (a, _) => {
                let (good, bad) = if a.is_some() { (col_a, col_b) } else { (col_b, col_a) };
                return Err(Error::InvalidParameter {
                    name: "pairs",
                    reason: format!(
                        "line {}: column {good} parses but column {bad} does not — refusing \
                         to pair values across rows (fix or drop the line)",
                        i + 1,
                    ),
                });
            }
        }
    }
    Ok(pairs)
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
        let probs = cal.calibrate(&grid).unwrap();
        for w in probs.windows(2) {
            assert!(w[1] >= w[0] - 1e-9, "calibrator must be monotone");
        }
        assert!(cal.probability(0.2).unwrap() < 0.1, "low scores → low prob");
        assert!(cal.probability(0.8).unwrap() > 0.9, "high scores → high prob");
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
        let after = brier_score(&cal.calibrate(&raw).unwrap(), &outcomes).unwrap();
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
    fn probability_metrics_reject_out_of_range_predictions() {
        // A "probability" of 1.5 used to be binned clamped but Brier-scored
        // raw — two inconsistent readings from one input. Now both reject.
        assert!(brier_score(&[0.5, 1.5], &[true, false]).is_err());
        assert!(brier_score(&[-0.1], &[true]).is_err());
        assert!(reliability(&[0.5, 1.5], &[true, false], 5).is_err());
        // The boundaries themselves are valid probabilities.
        assert!(brier_score(&[0.0, 1.0], &[false, true]).is_ok());
        assert!(reliability(&[0.0, 1.0], &[false, true], 5).is_ok());
    }

    #[test]
    fn tied_scores_get_one_pooled_value_invariant_to_input_order() {
        // Isotonic regression is a function of x: equal scores must receive
        // equal fitted values, whatever the arrival order of their outcomes.
        // Without secondary pooling, [F,T] gave p(0.5)=0.0 and [T,F] p(0.5)=0.5.
        let a = Calibrator::fit_isotonic(&[0.5, 0.5], &[false, true]).unwrap();
        let b = Calibrator::fit_isotonic(&[0.5, 0.5], &[true, false]).unwrap();
        assert_eq!(a.probability(0.5).unwrap(), 0.5);
        assert_eq!(b.probability(0.5).unwrap(), 0.5);

        // Interior tie block: the pooled value is the tie group's mean (0.5),
        // not the 1.0 the unpooled PAV used to report (factor-2 error).
        let scores = [0.1, 0.5, 0.5, 0.5, 0.5, 0.9];
        let outcomes = [false, false, false, true, true, true];
        let cal = Calibrator::fit_isotonic(&scores, &outcomes).unwrap();
        assert_eq!(cal.probability(0.5).unwrap(), 0.5);
        // And a shuffled copy of the same multiset fits identically.
        let scores_sh = [0.5, 0.9, 0.5, 0.1, 0.5, 0.5];
        let outcomes_sh = [true, true, false, false, true, false];
        let cal_sh = Calibrator::fit_isotonic(&scores_sh, &outcomes_sh).unwrap();
        for s in [0.0, 0.1, 0.3, 0.5, 0.7, 0.9, 1.0] {
            assert_eq!(cal.probability(s).unwrap(), cal_sh.probability(s).unwrap());
        }
    }

    #[test]
    fn probability_rejects_non_finite_scores_instead_of_panicking() {
        // A NaN here used to hit `partial_cmp().unwrap()` inside the binary
        // search — a panic reachable straight from the Python binding.
        let cal = Calibrator::fit_isotonic(&[0.1, 0.5, 0.9], &[false, true, true]).unwrap();
        assert!(cal.probability(f64::NAN).is_err());
        assert!(cal.probability(f64::INFINITY).is_err());
        assert!(cal.calibrate(&[0.5, f64::NAN]).is_err());
        // Finite scores keep working, including outside the fitted range.
        assert!(cal.probability(0.5).is_ok());
        assert_eq!(cal.probability(-10.0).unwrap(), cal.probability(0.0).unwrap());
    }

    #[test]
    fn csv_pairs_refuses_crossed_parse_failures() {
        // Header tolerated; clean rows pair up.
        let clean = "score,outcome\n0.9,1\n0.1,0\n";
        assert_eq!(csv_pairs(clean, 0, 1).unwrap(), vec![(0.9, 1.0), (0.1, 0.0)]);
        // Crossed failures with equal per-column counts used to pair values
        // from different rows silently; now they are a hard error.
        let crossed = "score,outcome\n0.90,abc\nxyz,1\n0.10,0\n";
        let e = csv_pairs(crossed, 0, 1).unwrap_err().to_string();
        assert!(e.contains("line 2"), "should point at the first bad line: {e}");
        // Non-finite fields count as unparseable (same policy as csv_column).
        assert!(csv_pairs("NaN,1\n", 0, 1).is_err());
        // A missing trailing field is an error too, not a shifted pair.
        assert!(csv_pairs("0.5\n", 0, 1).is_err());
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
