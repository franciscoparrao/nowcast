//! Ensemble (probabilistic) nowcasting.
//!
//! A deterministic forcing gives one hazard field per step. A *forecast* forcing —
//! e.g. an ensemble rainfall nowcast (pySTEPS, a deep generative model such as
//! DGMR) — gives many plausible realizations of the near future. Running the
//! engine on each ensemble member and aggregating yields a **probabilistic
//! hazard**: at every cell and step, the fraction of members that cross the alert
//! level (the exceedance probability), plus the ensemble mean and spread.
//!
//! This is the engine-side machinery for SOTA forecast forcing: the members
//! themselves come through the ordinary [`Forcing`](crate::Forcing) interface, so
//! a real ensemble QPF model plugs in without touching the hazard logic. The
//! exceedance probability is a genuine forecast probability with built-in
//! uncertainty (the spread), and feeds straight into the calibration and
//! reliability tools ([`crate::reliability`]).

use crate::error::{Error, Result};
use crate::forcing::Forcing;
use crate::grid::{GridDims, SusceptibilityMap};
use crate::nowcast::Nowcast;
use crate::threshold::IdThreshold;
use crate::trigger::TriggerModel;

/// Probabilistic hazard for one step, aggregated across ensemble members.
#[derive(Debug, Clone)]
pub struct EnsembleField {
    /// Time step index.
    pub step: usize,
    dims: GridDims,
    /// Fraction of members whose hazard reaches the alert level, per cell `[0,1]`.
    p_exceed: Vec<f64>,
    /// Ensemble-mean hazard per cell.
    mean: Vec<f64>,
    /// Ensemble standard deviation (population) of hazard per cell.
    spread: Vec<f64>,
}

impl EnsembleField {
    /// Grid of the field.
    pub fn dims(&self) -> GridDims {
        self.dims
    }

    /// Per-cell exceedance probability: fraction of members at/above the alert
    /// level. This is the probabilistic forecast hazard.
    pub fn probability_of_exceedance(&self) -> &[f64] {
        &self.p_exceed
    }

    /// Per-cell ensemble-mean hazard.
    pub fn mean(&self) -> &[f64] {
        &self.mean
    }

    /// Per-cell ensemble spread (population standard deviation).
    pub fn spread(&self) -> &[f64] {
        &self.spread
    }

    /// Highest exceedance probability anywhere on the grid this step.
    pub fn max_probability_of_exceedance(&self) -> f64 {
        self.p_exceed.iter().copied().fold(0.0_f64, f64::max)
    }
}

/// Run the engine over an ensemble of forcing members (each consumed through the
/// ordinary [`Forcing`] interface) sharing one susceptibility, threshold and
/// trigger, and aggregate into a per-step probabilistic hazard.
///
/// `alert_level` defines exceedance (hazard `>=` level). All members must share
/// the susceptibility grid and step count. Errors on an empty ensemble or a
/// member whose grid or length disagrees.
pub fn ensemble_hazard<F: Forcing>(
    susceptibility: &SusceptibilityMap,
    members: Vec<F>,
    threshold: IdThreshold,
    trigger: TriggerModel,
    max_window_steps: usize,
    alert_level: f64,
) -> Result<Vec<EnsembleField>> {
    let m = members.len();
    if m == 0 {
        return Err(Error::InvalidParameter {
            name: "members",
            reason: "ensemble needs at least one member".into(),
        });
    }
    let dims = susceptibility.dims();
    let n_cells = dims.len();
    let n_steps = members[0].n_steps();

    // Run each member; accumulate per (step, cell) the count over the level, the
    // sum and the sum of squares (for mean and population spread).
    let mut count = vec![vec![0u32; n_cells]; n_steps];
    let mut sum = vec![vec![0.0_f64; n_cells]; n_steps];
    let mut sumsq = vec![vec![0.0_f64; n_cells]; n_steps];

    for member in members {
        if member.n_steps() != n_steps {
            return Err(Error::InvalidParameter {
                name: "members",
                reason: format!("member has {} steps, expected {n_steps}", member.n_steps()),
            });
        }
        // `Nowcast::new` validates the grid match against the susceptibility.
        let nc = Nowcast::new(susceptibility.clone(), member, threshold, trigger, max_window_steps)?;
        for field in nc.run() {
            let t = field.step;
            for (c, &h) in field.probability().iter().enumerate() {
                if h >= alert_level {
                    count[t][c] += 1;
                }
                sum[t][c] += h;
                sumsq[t][c] += h * h;
            }
        }
    }

    let mf = m as f64;
    let fields = (0..n_steps)
        .map(|t| {
            let p_exceed = count[t].iter().map(|&k| k as f64 / mf).collect();
            let mean: Vec<f64> = sum[t].iter().map(|&s| s / mf).collect();
            let spread = mean
                .iter()
                .zip(&sumsq[t])
                .map(|(&mu, &sq)| (sq / mf - mu * mu).max(0.0).sqrt())
                .collect();
            EnsembleField { step: t, dims, p_exceed, mean, spread }
        })
        .collect();
    Ok(fields)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::forcing::UniformRain;

    fn susc(dims: GridDims) -> SusceptibilityMap {
        SusceptibilityMap::uniform(dims, 0.9).unwrap()
    }

    #[test]
    fn single_member_is_deterministic() {
        let dims = GridDims::new(1, 1);
        let m = UniformRain::new(dims, 24.0, vec![5.0, 80.0, 2.0]).unwrap();
        let fields = ensemble_hazard(
            &susc(dims),
            vec![m],
            IdThreshold::new(6.0, 0.39).unwrap(),
            TriggerModel::default(),
            7,
            0.5,
        )
        .unwrap();
        assert_eq!(fields.len(), 3);
        for f in &fields {
            // One member: spread 0, p_exceed is exactly 0 or 1.
            assert_eq!(f.spread()[0], 0.0);
            let p = f.probability_of_exceedance()[0];
            assert!(p == 0.0 || p == 1.0);
        }
    }

    #[test]
    fn exceedance_is_member_fraction() {
        let dims = GridDims::new(1, 1);
        // Three members with increasing peak: 0, 1 and 2 of them should cross.
        let dry = UniformRain::new(dims, 24.0, vec![1.0]).unwrap();
        let wet = UniformRain::new(dims, 24.0, vec![90.0]).unwrap();
        let wetter = UniformRain::new(dims, 24.0, vec![120.0]).unwrap();
        let fields = ensemble_hazard(
            &susc(dims),
            vec![dry, wet, wetter],
            IdThreshold::new(6.0, 0.39).unwrap(),
            TriggerModel::default(),
            7,
            0.5,
        )
        .unwrap();
        let p = fields[0].probability_of_exceedance()[0];
        assert!((p - 2.0 / 3.0).abs() < 1e-9, "two of three members cross, got {p}");
        assert!(fields[0].spread()[0] > 0.0, "mixed members have spread");
    }

    #[test]
    fn rejects_empty_and_mismatched() {
        let dims = GridDims::new(1, 1);
        let empty: Vec<UniformRain> = vec![];
        assert!(
            ensemble_hazard(&susc(dims), empty, IdThreshold::caine(), TriggerModel::default(), 7, 0.5)
                .is_err()
        );
        let a = UniformRain::new(dims, 24.0, vec![1.0, 2.0]).unwrap();
        let b = UniformRain::new(dims, 24.0, vec![1.0]).unwrap(); // different length
        assert!(
            ensemble_hazard(&susc(dims), vec![a, b], IdThreshold::caine(), TriggerModel::default(), 7, 0.5)
                .is_err()
        );
    }
}
