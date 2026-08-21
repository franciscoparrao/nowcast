//! Empirical rainfall intensity–duration (I–D) thresholds.
//!
//! The classic power-law threshold (Caine, 1980 and many regional successors)
//! states that landslides become likely when the mean rainfall intensity `I`
//! over a duration `D` exceeds a critical curve:
//!
//! ```text
//! I_crit(D) = a · D^(-b)
//! ```
//!
//! with `I` in mm/h and `D` in hours. `a` sets the overall level and `b` the
//! decay with duration. The *exceedance ratio* `E = I_obs / I_crit(D)` is the
//! dimensionless distance to the curve: `E < 1` below threshold, `E = 1` on it,
//! `E > 1` above.

use crate::error::{Error, Result};

/// A power-law intensity–duration threshold `I_crit(D) = a · D^(-b)`.
#[derive(Debug, Clone, Copy)]
pub struct IdThreshold {
    /// Intercept `a` (mm/h at D = 1 h). Must be > 0.
    pub a: f64,
    /// Decay exponent `b` (dimensionless). Must be >= 0.
    pub b: f64,
}

impl IdThreshold {
    pub fn new(a: f64, b: f64) -> Result<Self> {
        if !a.is_finite() || a <= 0.0 {
            return Err(Error::InvalidParameter {
                name: "a",
                reason: format!("I-D intercept must be finite and > 0, got {a}"),
            });
        }
        if !b.is_finite() || b < 0.0 {
            return Err(Error::InvalidParameter {
                name: "b",
                reason: format!("I-D exponent must be finite and >= 0, got {b}"),
            });
        }
        Ok(Self { a, b })
    }

    /// Caine (1980) global threshold: `I = 14.82 · D^(-0.39)` (mm/h, D in hours).
    ///
    /// A reasonable, well-known default to validate the susceptibility × trigger
    /// logic before a region-specific curve is calibrated.
    pub fn caine() -> Self {
        Self {
            a: 14.82,
            b: 0.39,
        }
    }

    /// Critical mean intensity (mm/h) for a given duration (hours).
    pub fn critical_intensity(&self, duration_h: f64) -> f64 {
        self.a * duration_h.powf(-self.b)
    }

    /// Mean intensity (mm/h) that yields exceedance `e` at duration `duration_h`
    /// — i.e. `e · I_crit(D)`. Used for counterfactual explanations.
    pub fn intensity_for_exceedance(&self, e: f64, duration_h: f64) -> f64 {
        e * self.critical_intensity(duration_h)
    }

    /// Exceedance ratio `E = I_obs / I_crit(D)` for an observed mean intensity
    /// (mm/h) sustained over `duration_h` hours. `E >= 1` means the threshold is
    /// met or exceeded.
    pub fn exceedance(&self, mean_intensity_mm_h: f64, duration_h: f64) -> f64 {
        let crit = self.critical_intensity(duration_h);
        if crit <= 0.0 {
            return 0.0;
        }
        mean_intensity_mm_h / crit
    }

    /// Worst (max) exceedance over rolling windows `m = 1..=max_m` ending at the
    /// current step, and the window length that produced it.
    ///
    /// `window_depth(m)` returns the accumulated depth (mm) over the trailing
    /// window of `m` steps; `dt_hours` is the step length. This is **the** I–D
    /// window kernel: the batch engine ([`Nowcast`](crate::Nowcast)), the
    /// composable [`IdTrigger`](crate::IdTrigger) and the streaming
    /// [`LiveNowcast`](crate::LiveNowcast) all call it, so the window scheme
    /// lives in exactly one place and every driver stays bit-identical (see the
    /// parity tests in `live` and `multi`).
    pub fn worst_window(
        &self,
        dt_hours: f64,
        max_m: usize,
        window_depth: impl Fn(usize) -> f64,
    ) -> (f64, usize) {
        let (mut best_e, mut best_m) = (0.0_f64, 1usize);
        for m in 1..=max_m {
            let duration_h = m as f64 * dt_hours;
            let e = self.exceedance(window_depth(m) / duration_h, duration_h);
            if e > best_e {
                best_e = e;
                best_m = m;
            }
        }
        (best_e, best_m)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A storm profile with a short burst inside a long wet spell, so the
    /// dominant window is a genuine choice among competing durations.
    const BURST: [f64; 8] = [0.5, 0.5, 2.0, 18.0, 22.0, 3.0, 0.5, 0.5];

    fn depth(m: usize) -> f64 {
        BURST[BURST.len() - m..].iter().sum()
    }

    /// `E = I·D^b / a` — exceedance is exactly inversely proportional to the
    /// intercept. The `intercept_sensitivity` example and the critical-intercept
    /// definition `a_crit = max I·D^b` both rest on this, so it is pinned here.
    #[test]
    fn exceedance_scales_inversely_with_intercept() {
        for &a in &[1.0, 4.0, 5.5, 14.82, 176.4] {
            let th = IdThreshold::new(a, 0.39).unwrap();
            let e = th.exceedance(12.0, 3.0);
            let unit = IdThreshold::new(1.0, 0.39).unwrap().exceedance(12.0, 3.0);
            assert!(
                (e * a - unit).abs() < 1e-12,
                "E({a}) should equal E(1)/{a}, got {e} vs {}",
                unit / a
            );
        }
    }

    /// The window that maximises exceedance is the SAME for every intercept
    /// (`a` is a common factor), which is what makes `a_crit` well defined —
    /// it is a property of the rainfall, not of the threshold chosen.
    #[test]
    fn worst_window_picks_same_window_for_any_intercept() {
        let reference = IdThreshold::new(1.0, 0.39)
            .unwrap()
            .worst_window(0.5, BURST.len(), depth);
        for &a in &[0.5, 4.0, 5.5, 14.82, 1000.0] {
            let (e, m) = IdThreshold::new(a, 0.39)
                .unwrap()
                .worst_window(0.5, BURST.len(), depth);
            assert_eq!(m, reference.1, "dominant window moved at a = {a}");
            assert!(
                (e * a - reference.0).abs() < 1e-9,
                "worst exceedance should scale as 1/a at a = {a}"
            );
        }
    }

    /// Hostile: a dry series has no critical intercept — the worst exceedance is
    /// zero at every intercept, so no threshold, however low, is crossed.
    #[test]
    fn dry_series_never_crosses_at_any_intercept() {
        for &a in &[1e-6, 1.0, 14.82] {
            let (e, _) = IdThreshold::new(a, 0.39)
                .unwrap()
                .worst_window(0.5, 4, |_| 0.0);
            assert_eq!(e, 0.0, "dry series crossed at a = {a}");
        }
    }
}
