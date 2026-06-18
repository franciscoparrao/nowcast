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
}
