//! The nowcast engine: combine static susceptibility with a dynamic trigger to
//! produce a time-varying hazard-probability field and threshold alerts.
//!
//! For each time step `t` and cell `c`:
//!
//! 1. Over rolling windows of `m = 1..=max_window_steps` ending at `t`,
//!    accumulate the water-input depth, form the mean intensity
//!    `I = depth / (m · dt)` and duration `D = m · dt`, and evaluate the I–D
//!    exceedance `E_m`. Take the worst (max) exceedance over all window lengths.
//! 2. Map that exceedance to a trigger factor `T ∈ [0, 1]`.
//! 3. Modulate susceptibility: `P(c, t) = susceptibility(c) · T`.
//!
//! Step 1 uses per-cell prefix sums so each window sum is O(1); the whole run is
//! O(cells · steps · max_window).

use crate::error::{Error, Result};
use crate::forcing::Forcing;
use crate::grid::{GridDims, SusceptibilityMap};
use crate::threshold::IdThreshold;
use crate::trigger::TriggerModel;

/// Hazard-probability field for one time step (row-major, one value per cell).
#[derive(Debug, Clone)]
pub struct HazardField {
    pub step: usize,
    dims: GridDims,
    probability: Vec<f64>,
}

impl HazardField {
    /// Build a hazard field from a row-major probability vector.
    ///
    /// This is the general `susceptibility × trigger` combiner output and is
    /// public so other hazard paths (e.g. the flood provider's discharge trigger
    /// in `nowcast-rainflow`) can produce the same field type. Fails if the
    /// length does not match `dims` or any value lies outside `[0, 1]`.
    pub fn new(step: usize, dims: GridDims, probability: Vec<f64>) -> Result<Self> {
        if probability.len() != dims.len() {
            return Err(Error::GridSizeMismatch {
                expected: dims.len(),
                got: probability.len(),
                ncols: dims.ncols,
                nrows: dims.nrows,
            });
        }
        if let Some((cell, v)) = probability
            .iter()
            .enumerate()
            .find(|(_, v)| !(0.0..=1.0).contains(*v))
        {
            return Err(Error::InvalidParameter {
                name: "probability",
                reason: format!("hazard at cell {cell} is {v}, expected within [0, 1]"),
            });
        }
        Ok(Self {
            step,
            dims,
            probability,
        })
    }

    pub fn dims(&self) -> GridDims {
        self.dims
    }

    pub fn probability(&self) -> &[f64] {
        &self.probability
    }

    /// Largest hazard probability anywhere on the field at this step.
    pub fn max_probability(&self) -> f64 {
        self.probability.iter().copied().fold(0.0, f64::max)
    }

    /// An [`Alert`] for this step if any cell reaches `level`, else `None`.
    pub fn alert(&self, level: f64) -> Option<Alert> {
        let n_cells = self.probability.iter().filter(|&&p| p >= level).count();
        (n_cells > 0).then(|| Alert {
            step: self.step,
            n_cells,
            fraction: n_cells as f64 / self.dims.len() as f64,
            max_probability: self.max_probability(),
        })
    }
}

/// A step whose hazard field reached or exceeded the alert level somewhere.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Alert {
    pub step: usize,
    /// Number of cells at or above the alert level.
    pub n_cells: usize,
    /// Fraction of the grid at or above the alert level, in `[0, 1]`.
    pub fraction: f64,
    /// Peak hazard probability at this step.
    pub max_probability: f64,
}

/// The combined susceptibility × trigger nowcast engine.
pub struct Nowcast<F: Forcing> {
    susceptibility: SusceptibilityMap,
    forcing: F,
    threshold: IdThreshold,
    trigger: TriggerModel,
    max_window_steps: usize,
}

impl<F: Forcing> Nowcast<F> {
    /// Build a nowcast. The forcing grid must match the susceptibility grid.
    ///
    /// `max_window_steps` bounds the longest rolling I–D window considered; it
    /// is clamped to the number of available steps.
    pub fn new(
        susceptibility: SusceptibilityMap,
        forcing: F,
        threshold: IdThreshold,
        trigger: TriggerModel,
        max_window_steps: usize,
    ) -> Result<Self> {
        let sd = susceptibility.dims();
        let fd = forcing.dims();
        if sd != fd {
            return Err(Error::GridMismatch {
                susc_cols: sd.ncols,
                susc_rows: sd.nrows,
                forc_cols: fd.ncols,
                forc_rows: fd.nrows,
            });
        }
        if max_window_steps == 0 {
            return Err(Error::InvalidParameter {
                name: "max_window_steps",
                reason: "must be >= 1".to_string(),
            });
        }
        Ok(Self {
            susceptibility,
            forcing,
            threshold,
            trigger,
            max_window_steps,
        })
    }

    fn n_cells(&self) -> usize {
        self.susceptibility.dims().len()
    }

    /// Per-cell prefix sums of water-input depth: `prefix[c][t]` is the total
    /// depth over steps `0..t` (so `prefix[c]` has length `n_steps + 1`).
    fn depth_prefix_sums(&self) -> Vec<Vec<f64>> {
        let n_steps = self.forcing.n_steps();
        let n_cells = self.n_cells();
        let mut prefix = vec![vec![0.0; n_steps + 1]; n_cells];
        for (c, row) in prefix.iter_mut().enumerate() {
            for t in 0..n_steps {
                row[t + 1] = row[t] + self.forcing.depth_mm(c, t);
            }
        }
        prefix
    }

    /// Worst I–D exceedance for cell `c` at step `t`, over all rolling window
    /// lengths up to `max_window_steps`, using precomputed prefix sums.
    fn max_exceedance(&self, prefix: &[Vec<f64>], c: usize, t: usize) -> f64 {
        let dt = self.forcing.dt_hours();
        let max_m = self.max_window_steps.min(t + 1);
        let row = &prefix[c];
        let mut best = 0.0_f64;
        for m in 1..=max_m {
            let window_depth = row[t + 1] - row[t + 1 - m];
            let duration_h = m as f64 * dt;
            let mean_intensity = window_depth / duration_h;
            let e = self.threshold.exceedance(mean_intensity, duration_h);
            if e > best {
                best = e;
            }
        }
        best
    }

    /// Compute the hazard field for a single time step.
    pub fn hazard_at(&self, step: usize) -> HazardField {
        let prefix = self.depth_prefix_sums();
        self.hazard_at_with(&prefix, step)
    }

    fn hazard_at_with(&self, prefix: &[Vec<f64>], step: usize) -> HazardField {
        let dims = self.susceptibility.dims();
        let mut probability = vec![0.0; dims.len()];
        for (c, p) in probability.iter_mut().enumerate() {
            let e = self.max_exceedance(prefix, c, step);
            *p = self.susceptibility.get(c) * self.trigger.factor(e);
        }
        HazardField {
            step,
            dims,
            probability,
        }
    }

    /// Run the full nowcast, returning one [`HazardField`] per time step.
    pub fn run(&self) -> Vec<HazardField> {
        let prefix = self.depth_prefix_sums();
        (0..self.forcing.n_steps())
            .map(|t| self.hazard_at_with(&prefix, t))
            .collect()
    }

    /// Run the nowcast and emit an [`Alert`] for every step whose peak hazard
    /// reaches `level` (in `[0, 1]`).
    pub fn alerts(&self, level: f64) -> Vec<Alert> {
        self.run()
            .into_iter()
            .filter_map(|field| field.alert(level))
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::forcing::UniformRain;

    fn single_cell(depths: Vec<f64>, susc: f64) -> Nowcast<UniformRain> {
        let dims = GridDims::new(1, 1);
        let forcing = UniformRain::new(dims, 1.0, depths).unwrap();
        let susceptibility = SusceptibilityMap::uniform(dims, susc).unwrap();
        Nowcast::new(
            susceptibility,
            forcing,
            IdThreshold::caine(),
            TriggerModel::default(),
            24,
        )
        .unwrap()
    }

    #[test]
    fn dry_series_gives_near_zero_hazard() {
        let nc = single_cell(vec![0.0; 10], 0.9);
        let fields = nc.run();
        for f in &fields {
            assert!(f.max_probability() < 0.05, "dry step too hot: {f:?}");
        }
    }

    #[test]
    fn heavy_burst_exceeds_threshold() {
        // 40 mm in one hour: I = 40 mm/h, D = 1 h. Caine I_crit(1) = 14.82 mm/h,
        // so E ≈ 2.7 → trigger factor near 1 → hazard ≈ susceptibility.
        let nc = single_cell(vec![0.0, 0.0, 40.0, 0.0], 0.8);
        let fields = nc.run();
        let peak = &fields[2];
        assert!(
            peak.max_probability() > 0.7,
            "expected hazard near susceptibility, got {}",
            peak.max_probability()
        );
        // Hazard relaxes after the burst passes out of the short windows.
        assert!(fields[3].max_probability() < peak.max_probability());
    }

    #[test]
    fn on_threshold_gives_half_susceptibility() {
        // Exactly Caine's critical 1-hour intensity → E = 1 → factor 0.5.
        let crit_1h = IdThreshold::caine().critical_intensity(1.0);
        let nc = single_cell(vec![crit_1h], 0.6);
        let p = nc.hazard_at(0).max_probability();
        assert!((p - 0.3).abs() < 1e-6, "expected 0.5 * 0.6 = 0.3, got {p}");
    }

    #[test]
    fn zero_susceptibility_never_alerts() {
        let nc = single_cell(vec![100.0, 100.0], 0.0);
        assert!(nc.alerts(0.1).is_empty());
    }

    #[test]
    fn alerts_start_at_burst_and_persist_via_antecedent_rain() {
        // A single 50 mm hour. The burst step alerts, and alerts persist into
        // following dry steps because longer rolling windows still carry that
        // depth above the I-D curve (antecedent rainfall) — the intended,
        // physically meaningful behaviour of the multi-window scheme.
        let nc = single_cell(vec![0.0, 50.0, 0.0, 0.0], 0.9);
        let alerts = nc.alerts(0.5);
        let steps: Vec<usize> = alerts.iter().map(|a| a.step).collect();

        assert!(!steps.contains(&0), "no rain has fallen yet at step 0");
        assert!(steps.contains(&1), "the 50 mm burst must alert");
        assert_eq!(*steps.first().unwrap(), 1, "first alert is the burst step");
        // The peak is at the burst and relaxes afterwards as the window dilutes.
        assert!(alerts[0].max_probability >= alerts.last().unwrap().max_probability);
        assert_eq!(alerts[0].n_cells, 1);
        assert!((alerts[0].fraction - 1.0).abs() < 1e-9);
    }

    #[test]
    fn grid_mismatch_is_rejected() {
        let forcing = UniformRain::new(GridDims::new(2, 2), 1.0, vec![1.0]).unwrap();
        let susc = SusceptibilityMap::uniform(GridDims::new(3, 3), 0.5).unwrap();
        let err = Nowcast::new(
            susc,
            forcing,
            IdThreshold::caine(),
            TriggerModel::default(),
            6,
        );
        assert!(err.is_err());
    }

    #[test]
    fn spatial_field_modulates_by_susceptibility() {
        // Two cells, different susceptibility, same (heavy) uniform rain.
        let dims = GridDims::new(2, 1);
        let forcing = UniformRain::new(dims, 1.0, vec![50.0]).unwrap();
        let susc = SusceptibilityMap::new(dims, vec![0.2, 0.9]).unwrap();
        let nc = Nowcast::new(
            susc,
            forcing,
            IdThreshold::caine(),
            TriggerModel::default(),
            6,
        )
        .unwrap();
        let field = nc.hazard_at(0);
        let p = field.probability();
        assert!(p[1] > p[0], "higher susceptibility must yield higher hazard");
        // Same trigger factor on both cells → ratio equals susceptibility ratio.
        assert!((p[1] / p[0] - 0.9 / 0.2).abs() < 1e-9);
    }
}
