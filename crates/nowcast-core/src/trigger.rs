//! Mapping from I–D exceedance to a dimensionless trigger factor in `[0, 1]`.
//!
//! A hard threshold (`E >= 1` → fire, else nothing) is brittle: it throws away
//! how far past the curve the forcing reached and produces all-or-nothing maps.
//! Instead we map the exceedance ratio `E` through a logistic so the hazard
//! ramps smoothly across the threshold:
//!
//! ```text
//! T(E) = 1 / (1 + exp(-k · (E - 1)))
//! ```
//!
//! `T(1) = 0.5` exactly (on the curve), `T → 0` well below and `T → 1` well
//! above. `k` controls how sharp the transition is.

use crate::error::{Error, Result};

/// Logistic exceedance → trigger-factor model.
#[derive(Debug, Clone, Copy)]
pub struct TriggerModel {
    /// Steepness of the logistic transition. Larger `k` ≈ harder threshold.
    pub k: f64,
}

impl TriggerModel {
    pub fn new(k: f64) -> Result<Self> {
        if !k.is_finite() || k <= 0.0 {
            return Err(Error::InvalidParameter {
                name: "k",
                reason: format!("logistic steepness must be > 0, got {k}"),
            });
        }
        Ok(Self { k })
    }

    /// Trigger factor in `[0, 1]` for an exceedance ratio `E`.
    pub fn factor(&self, exceedance: f64) -> f64 {
        1.0 / (1.0 + (-self.k * (exceedance - 1.0)).exp())
    }
}

impl Default for TriggerModel {
    /// `k = 4.0`: a moderately soft threshold (T ≈ 0.018 at E = 0, 0.5 at E = 1,
    /// 0.98 at E = 2).
    fn default() -> Self {
        Self { k: 4.0 }
    }
}
