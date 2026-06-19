//! # nowcast-insar
//!
//! Ground **deformation as a second trigger**. Rainfall says a slope is being
//! loaded; InSAR says it is already moving. `insar-rs` produces a LOS mean
//! velocity field (PS-InSAR / SBAS); this crate turns it into a
//! [`nowcast_core::Forcing`] so it can drive a [`ThresholdTrigger`] and be fused
//! with the rainfall I–D trigger in a [`MultiNowcast`]:
//!
//! ```text
//!     hazard = susceptibility × ( rain_I-D  ⊕  deformation_rate )      (noisy-OR)
//! ```
//!
//! Pre-failure creep is a quasi-stationary background, so the velocity field is
//! **broadcast over the nowcast's time steps**: a fast-moving cell sits closer
//! to failure and needs less rain to alert. Magnitude `|v|` is used (both
//! subsidence and uplift signal instability); NaN/incoherent cells → 0.
//!
//! Build a deformation trigger and combine it with rain:
//!
//! ```
//! use ndarray::Array2;
//! use nowcast_core::{Combine, IdThreshold, IdTrigger, MultiNowcast, SusceptibilityMap, TriggerModel, UniformRain};
//! use nowcast_insar::deformation_trigger_from_velocity;
//!
//! let dims = nowcast_core::GridDims::new(2, 1);
//! // 30 mm/yr LOS velocity on cell 1 (m/yr in the raster).
//! let vel = Array2::from_shape_vec((1, 2), vec![0.002_f32, 0.030]).unwrap();
//! let deform = deformation_trigger_from_velocity(&vel, 3, 24.0, 20.0, TriggerModel::default()).unwrap();
//!
//! let rain = IdTrigger::new(UniformRain::new(dims, 24.0, vec![0.0, 0.0, 0.0]).unwrap(),
//!     IdThreshold::caine(), TriggerModel::default(), 3).unwrap();
//! let susc = SusceptibilityMap::uniform(dims, 0.8).unwrap();
//! let nc = MultiNowcast::new(susc, vec![Box::new(rain), Box::new(deform)], Combine::NoisyOr).unwrap();
//! // Cell 1 (fast creep) alerts even with no rain.
//! assert!(nc.hazard_at(0).probability()[1] > nc.hazard_at(0).probability()[0]);
//! ```

use ndarray::Array2;
use nowcast_core::{Forcing, GridDims, Result, ThresholdTrigger, TriggerModel};

// Re-export the insar-rs entry points so callers need only this crate.
pub use insar_core::pipeline::{SbasPipelineConfig, SbasProducts, run_sbas};
pub use insar_core::types::{DisplacementSeries, VelocityMap};

/// A per-cell LOS deformation **rate** (mm/yr), exposed as a [`Forcing`] that is
/// constant in time (broadcast over every step) — the quasi-stationary creep
/// background that biases the trigger.
#[derive(Debug, Clone)]
pub struct DeformationForcing {
    dims: GridDims,
    n_steps: usize,
    dt_hours: f64,
    /// `|velocity|` per cell, mm/yr, row-major.
    rate_mm_yr: Vec<f64>,
}

impl DeformationForcing {
    /// From a LOS velocity field in **m/yr** (insar-rs convention). Converts to
    /// mm/yr, takes magnitude, maps NaN/nodata to 0. `n_steps`/`dt_hours` should
    /// match the rainfall trigger it will be combined with.
    pub fn from_velocity_m_per_yr(velocity: &Array2<f32>, n_steps: usize, dt_hours: f64) -> Self {
        let (rows, cols) = velocity.dim();
        let mut rate_mm_yr = Vec::with_capacity(rows * cols);
        for i in 0..rows {
            for j in 0..cols {
                let v = velocity[[i, j]];
                rate_mm_yr.push(if v.is_finite() { v.abs() as f64 * 1000.0 } else { 0.0 });
            }
        }
        Self {
            dims: GridDims::new(cols, rows),
            n_steps,
            dt_hours,
            rate_mm_yr,
        }
    }

    /// From an insar-rs [`VelocityMap`] (LOS mean velocity, m/yr).
    pub fn from_velocity_map(map: &VelocityMap, n_steps: usize, dt_hours: f64) -> Self {
        Self::from_velocity_m_per_yr(&map.data, n_steps, dt_hours)
    }
}

impl Forcing for DeformationForcing {
    fn dims(&self) -> GridDims {
        self.dims
    }
    fn n_steps(&self) -> usize {
        self.n_steps
    }
    fn dt_hours(&self) -> f64 {
        self.dt_hours
    }
    fn depth_mm(&self, cell: usize, _step: usize) -> f64 {
        self.rate_mm_yr[cell] // broadcast over time
    }
}

/// Build a deformation [`ThresholdTrigger`] from a LOS velocity field (m/yr):
/// `factor = model(|v| / v_crit)`, with `v_crit` in mm/yr.
pub fn deformation_trigger_from_velocity(
    velocity: &Array2<f32>,
    n_steps: usize,
    dt_hours: f64,
    v_crit_mm_yr: f64,
    model: TriggerModel,
) -> Result<ThresholdTrigger<DeformationForcing>> {
    let forcing = DeformationForcing::from_velocity_m_per_yr(velocity, n_steps, dt_hours);
    ThresholdTrigger::new(forcing, v_crit_mm_yr, model)
}

/// Convenience over an insar-rs [`VelocityMap`].
pub fn deformation_trigger(
    map: &VelocityMap,
    n_steps: usize,
    dt_hours: f64,
    v_crit_mm_yr: f64,
    model: TriggerModel,
) -> Result<ThresholdTrigger<DeformationForcing>> {
    deformation_trigger_from_velocity(&map.data, n_steps, dt_hours, v_crit_mm_yr, model)
}

#[cfg(test)]
mod tests {
    use super::*;
    use nowcast_core::{
        Combine, IdThreshold, IdTrigger, MultiNowcast, SusceptibilityMap, UniformRain,
    };

    #[test]
    fn velocity_becomes_rate_mm_yr() {
        // 2x2 m/yr field with a NaN; expect mm/yr magnitudes, NaN→0.
        let v = Array2::from_shape_vec((2, 2), vec![0.005_f32, -0.012, f32::NAN, 0.0]).unwrap();
        let f = DeformationForcing::from_velocity_m_per_yr(&v, 4, 24.0);
        assert_eq!(f.dims(), GridDims::new(2, 2));
        assert_eq!(f.n_steps(), 4);
        assert!((f.depth_mm(0, 0) - 5.0).abs() < 1e-6); // 0.005 m/yr → 5 mm/yr
        assert!((f.depth_mm(1, 0) - 12.0).abs() < 1e-6); // |−0.012| → 12
        assert_eq!(f.depth_mm(2, 2), 0.0); // NaN → 0
        // broadcast over time
        assert_eq!(f.depth_mm(0, 0), f.depth_mm(0, 3));
    }

    #[test]
    fn deformation_lets_a_creeping_cell_alert_without_rain() {
        let dims = GridDims::new(2, 1); // cell 0 stable, cell 1 creeping
        let vel = Array2::from_shape_vec((1, 2), vec![0.001_f32, 0.040]).unwrap(); // 1 vs 40 mm/yr
        let deform =
            deformation_trigger_from_velocity(&vel, 3, 24.0, 20.0, TriggerModel::default()).unwrap();
        let rain = IdTrigger::new(
            UniformRain::new(dims, 24.0, vec![0.0, 0.0, 0.0]).unwrap(), // bone dry
            IdThreshold::caine(),
            TriggerModel::default(),
            3,
        )
        .unwrap();
        let susc = SusceptibilityMap::uniform(dims, 0.85).unwrap();
        let nc = MultiNowcast::new(
            susc,
            vec![Box::new(rain), Box::new(deform)],
            Combine::NoisyOr,
        )
        .unwrap();
        let p = nc.hazard_at(0);
        assert!(p.probability()[0] < 0.1, "stable cell stays quiet");
        assert!(p.probability()[1] > 0.7, "creeping cell alerts on deformation alone");
    }
}
